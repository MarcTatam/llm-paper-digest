"""Tests for the arXiv daily digest job.

Run from the digest_job directory:  pytest test_main.py
or from repo root:                   pytest digest_job/test_main.py

External dependencies (arXiv HTTP, Claude, Firestore, Telegram) are mocked.
Pure logic (Markdown escaping, PDF truncation, XML parsing, formatting,
message chunking) is tested directly.
"""

import asyncio
import io
from unittest.mock import MagicMock, patch

import anthropic
import pytest
from pypdf import PdfReader, PdfWriter

import main
from main import (
    Paper,
    PaperSelection,
    PaperSummary,
    Profile,
    _escape_md,
    _truncate_pdf,
    fetch_arxiv_papers,
    fetch_latest_profile,
    format_telegram_digest,
    process_paper,
    rank_papers_with_claude,
    save_paper_to_firestore,
    send_digest,
    send_telegram_message,
)


# --- helpers ----------------------------------------------------------------

def make_pdf(num_pages: int) -> bytes:
    """Build an in-memory PDF with the given number of blank pages."""
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_paper(i: int = 0, n_authors: int = 1) -> Paper:
    return Paper(
        arxiv_id=f"2401.{i:05d}",
        authors=[f"Author {j}" for j in range(n_authors)],
        title=f"Paper {i}",
        abstract=f"Abstract {i}",
        categories=["cs.AI"],
    )


# --- _escape_md -------------------------------------------------------------

class TestEscapeMd:
    def test_escapes_each_special_char(self):
        assert _escape_md("a_b") == "a\\_b"
        assert _escape_md("a*b") == "a\\*b"
        assert _escape_md("a`b") == "a\\`b"
        assert _escape_md("a[b") == "a\\[b"

    def test_escapes_multiple_chars(self):
        assert _escape_md("_*`[") == "\\_\\*\\`\\["

    def test_no_special_chars_unchanged(self):
        assert _escape_md("plain text 123") == "plain text 123"

    def test_empty_string(self):
        assert _escape_md("") == ""


# --- _truncate_pdf ----------------------------------------------------------

class TestTruncatePdf:
    def test_small_pdf_returned_unchanged(self):
        pdf = make_pdf(5)
        out = _truncate_pdf(pdf, head_pages=20, tail_pages=50)
        # Identical bytes object returned when no truncation needed.
        assert out == pdf

    def test_pdf_at_boundary_unchanged(self):
        # head + tail == total -> no truncation
        pdf = make_pdf(70)
        out = _truncate_pdf(pdf, head_pages=20, tail_pages=50)
        assert out == pdf

    def test_large_pdf_truncated(self):
        pdf = make_pdf(120)
        out = _truncate_pdf(pdf, head_pages=20, tail_pages=50)
        assert out != pdf
        assert len(PdfReader(io.BytesIO(out)).pages) == 70

    def test_custom_page_counts(self):
        pdf = make_pdf(100)
        out = _truncate_pdf(pdf, head_pages=5, tail_pages=5)
        assert len(PdfReader(io.BytesIO(out)).pages) == 10


# --- fetch_arxiv_papers -----------------------------------------------------

ARXIV_XML_MULTI = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v1</id>
    <title>First Paper</title>
    <summary>This is the first
abstract.</summary>
    <author><name>Alice</name></author>
    <author><name>Bob</name></author>
    <category term="cs.AI"/>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2401.00002v2</id>
    <title>Second Paper</title>
    <summary>Second abstract.</summary>
    <author><name>Carol</name></author>
    <category term="cs.CL"/>
  </entry>
</feed>"""

ARXIV_XML_SINGLE = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00003v1</id>
    <title>Lone Paper</title>
    <summary>Lone abstract.</summary>
    <author><name>Dave</name></author>
    <category term="cs.SE"/>
  </entry>
</feed>"""

ARXIV_XML_EMPTY = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""

ARXIV_XML_NO_ABSTRACT = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00004v1</id>
    <title>No Abstract Paper</title>
    <summary></summary>
    <author><name>Eve</name></author>
    <category term="cs.AI"/>
  </entry>
</feed>"""


def _mock_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


class TestFetchArxivPapers:
    def test_parses_multiple_entries(self):
        with patch.object(main.requests, "get", return_value=_mock_response(ARXIV_XML_MULTI)):
            papers = fetch_arxiv_papers(["cs.AI"], max_results=10)
        assert len(papers) == 2
        assert papers[0].title == "First Paper"
        assert papers[0].arxiv_id == "2401.00001v1"
        assert papers[0].authors == ["Alice", "Bob"]
        assert papers[0].categories == ["cs.AI", "cs.LG"]
        # newline in abstract collapsed to space
        assert papers[0].abstract == "This is the first abstract."

    def test_single_entry_handled_as_list(self):
        # arXiv returns a dict (not list) when there's one entry; code must coerce.
        with patch.object(main.requests, "get", return_value=_mock_response(ARXIV_XML_SINGLE)):
            papers = fetch_arxiv_papers(["cs.SE"], max_results=10)
        assert len(papers) == 1
        assert papers[0].title == "Lone Paper"
        assert papers[0].authors == ["Dave"]

    def test_empty_feed_returns_empty_list(self):
        with patch.object(main.requests, "get", return_value=_mock_response(ARXIV_XML_EMPTY)):
            papers = fetch_arxiv_papers(["cs.AI"], max_results=10)
        assert papers == []

    def test_entry_without_abstract_skipped(self):
        with patch.object(main.requests, "get", return_value=_mock_response(ARXIV_XML_NO_ABSTRACT)):
            papers = fetch_arxiv_papers(["cs.AI"], max_results=10)
        assert papers == []


# --- Paper URL helpers ------------------------------------------------------

class TestPaperUrls:
    def test_pdf_url(self):
        p = make_paper(1)
        assert p.get_pdf_url() == "https://arxiv.org/pdf/2401.00001.pdf"

    def test_abs_url(self):
        p = make_paper(1)
        assert p.get_abs_url() == "https://arxiv.org/abs/2401.00001"


# --- rank_papers_with_claude ------------------------------------------------

class TestRankPapers:
    def test_empty_input_returns_empty(self):
        assert rank_papers_with_claude([], top_n=5) == []

    def test_selection_indices_map_to_papers(self):
        papers = [make_paper(i) for i in range(5)]
        selection = PaperSelection(selection=[2, 0, 4])

        msg = MagicMock()
        msg.content = [MagicMock(text=selection.model_dump_json())]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = msg

        with patch.object(main, "fetch_latest_profile", return_value=None), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            result = rank_papers_with_claude(papers, top_n=5)

        assert [p.arxiv_id for p in result] == [
            papers[2].arxiv_id, papers[0].arxiv_id, papers[4].arxiv_id,
        ]

    def test_out_of_range_indices_filtered(self):
        papers = [make_paper(i) for i in range(3)]
        selection = PaperSelection(selection=[0, 99, -1, 2])

        msg = MagicMock()
        msg.content = [MagicMock(text=selection.model_dump_json())]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = msg

        with patch.object(main, "fetch_latest_profile", return_value=None), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            result = rank_papers_with_claude(papers, top_n=5)

        # Only valid indices 0 and 2 survive.
        assert [p.arxiv_id for p in result] == [papers[0].arxiv_id, papers[2].arxiv_id]

    def test_respects_top_n_cap(self):
        papers = [make_paper(i) for i in range(10)]
        selection = PaperSelection(selection=[0, 1, 2, 3, 4, 5])

        msg = MagicMock()
        msg.content = [MagicMock(text=selection.model_dump_json())]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = msg

        with patch.object(main, "fetch_latest_profile", return_value=None), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            result = rank_papers_with_claude(papers, top_n=3)

        assert len(result) == 3


# --- format_telegram_digest -------------------------------------------------

class TestFormatDigest:
    def test_produces_header_plus_one_per_paper(self):
        summaries = [
            (make_paper(i), PaperSummary(
                summary="s", application="a", prototype="p", impact="i",
            ))
            for i in range(3)
        ]
        messages = format_telegram_digest(summaries)
        # 1 header + 3 papers
        assert len(messages) == 4
        assert "arXiv Daily Digest" in messages[0]
        assert "3 papers selected" in messages[0]

    def test_paper_message_contains_links(self):
        paper = make_paper(7)
        summaries = [(paper, PaperSummary(
            summary="s", application="a", prototype="p", impact="i",
        ))]
        messages = format_telegram_digest(summaries)
        body = messages[1]
        assert paper.get_abs_url() in body
        assert paper.get_pdf_url() in body

    def test_author_truncation_et_al(self):
        paper = make_paper(1, n_authors=5)
        summaries = [(paper, PaperSummary(
            summary="s", application="a", prototype="p", impact="i",
        ))]
        messages = format_telegram_digest(summaries)
        assert "et al." in messages[1]

    def test_no_et_al_for_three_authors(self):
        paper = make_paper(1, n_authors=3)
        summaries = [(paper, PaperSummary(
            summary="s", application="a", prototype="p", impact="i",
        ))]
        messages = format_telegram_digest(summaries)
        assert "et al." not in messages[1]


# --- send_telegram_message (chunking + retry) -------------------------------

class TestSendTelegramMessage:
    def test_retries_as_plaintext_on_markdown_error(self, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", "chat")

        posts = []

        class FakeResp:
            def __init__(self, fail):
                self._fail = fail

            def raise_for_status(self):
                if self._fail:
                    raise main.httpx.HTTPStatusError("bad markdown", request=None, response=None)

            def json(self):
                return {"result": {"message_id": 777}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                posts.append(dict(json))  # copy: code mutates payload in place
                # First attempt (Markdown) fails; retry (empty parse_mode) succeeds.
                return FakeResp(fail=(json["parse_mode"] == "Markdown"))

        with patch.object(main.httpx, "AsyncClient", FakeClient):
            msg_id = asyncio.run(send_telegram_message("body"))

        assert msg_id == 777
        # Two posts: the failed Markdown attempt and the plain-text retry.
        assert len(posts) == 2
        assert posts[0]["parse_mode"] == "Markdown"
        assert posts[1]["parse_mode"] == ""

    def test_returns_false_without_credentials(self, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", None)
        monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", None)
        result = asyncio.run(send_telegram_message("hello"))
        assert result is False

    def test_short_message_single_post(self, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", "chat")

        posts = []

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": {"message_id": 555}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                posts.append(dict(json))
                return FakeResp()

        with patch.object(main.httpx, "AsyncClient", FakeClient):
            msg_id = asyncio.run(send_telegram_message("short message"))

        assert msg_id == 555
        assert len(posts) == 1

    def test_long_message_split_into_chunks(self, monkeypatch):
        monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setattr(main, "TELEGRAM_CHAT_ID", "chat")

        posts = []

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"result": {"message_id": 1}}

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json):
                posts.append(dict(json))
                return FakeResp()

        # Build text > 4000 chars across multiple paragraphs.
        long_text = "\n\n".join("x" * 1000 for _ in range(6))
        with patch.object(main.httpx, "AsyncClient", FakeClient):
            asyncio.run(send_telegram_message(long_text))

        assert len(posts) > 1
        # Every chunk must respect the 4000-char ceiling.
        assert all(len(p["text"]) <= 4000 for p in posts)


# --- fetch_latest_profile ---------------------------------------------------

class TestFetchLatestProfile:
    def test_returns_none_when_no_profiles(self):
        fake_client = MagicMock()
        fake_client.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = iter([])
        with patch.object(main.firestore, "Client", return_value=fake_client):
            assert fetch_latest_profile() is None

    def test_parses_latest_profile(self):
        doc = MagicMock()
        doc.to_dict.return_value = {
            "liked_themes": ["agents"],
            "disliked_themes": ["benchmarks"],
            "prose_summary": "Likes practical AI.",
        }
        fake_client = MagicMock()
        fake_client.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = iter([doc])
        with patch.object(main.firestore, "Client", return_value=fake_client):
            profile = fetch_latest_profile()
        assert profile is not None
        assert profile.liked_themes == ["agents"]
        assert profile.disliked_themes == ["benchmarks"]


# --- rank_papers_with_claude: profile-driven branch -------------------------

class TestRankPapersWithProfile:
    def test_profile_themes_injected_into_prompt(self):
        papers = [make_paper(i) for i in range(3)]
        profile = Profile(
            liked_themes=["RAG", "agents"],
            disliked_themes=["pure theory"],
            prose_summary="Cares about production AI systems.",
        )
        selection = PaperSelection(selection=[0])

        captured = {}

        def fake_create(*args, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            msg = MagicMock()
            msg.content = [MagicMock(text=selection.model_dump_json())]
            return msg

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = fake_create

        with patch.object(main, "fetch_latest_profile", return_value=profile), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            rank_papers_with_claude(papers, top_n=5)

        prompt = captured["prompt"]
        assert "Cares about production AI systems." in prompt
        assert "RAG" in prompt
        assert "pure theory" in prompt

    def test_empty_disliked_themes_renders_none(self):
        papers = [make_paper(0)]
        profile = Profile(
            liked_themes=["evals"],
            disliked_themes=[],
            prose_summary="Summary.",
        )
        selection = PaperSelection(selection=[0])

        captured = {}

        def fake_create(*args, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            msg = MagicMock()
            msg.content = [MagicMock(text=selection.model_dump_json())]
            return msg

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = fake_create

        with patch.object(main, "fetch_latest_profile", return_value=profile), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            rank_papers_with_claude(papers, top_n=5)

        # The "(none)" placeholder is used when there are no disliked themes.
        assert "(none)" in captured["prompt"]


# --- process_paper ----------------------------------------------------------

class FakeBadRequest(anthropic.BadRequestError):
    """A BadRequestError whose str() we control, avoiding the real constructor
    which needs a live httpx.Response."""

    def __init__(self, message: str):
        self._message = message

    def __str__(self):
        return self._message


class TestProcessPaper:
    def test_happy_path_no_truncation(self):
        paper = make_paper(1)
        summary = PaperSummary(summary="s", application="a", prototype="p", impact="i")

        pdf_resp = MagicMock()
        pdf_resp.content = make_pdf(3)

        msg = MagicMock()
        msg.content = [MagicMock(text=summary.model_dump_json())]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = msg

        with patch.object(main.time, "sleep"), \
             patch.object(main.requests, "get", return_value=pdf_resp), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            result = process_paper(paper)

        assert result.summary == "s"
        # Only one Claude call when the PDF is within page limits.
        assert fake_client.messages.create.call_count == 1

    def test_retries_with_truncation_on_page_limit(self):
        paper = make_paper(2)
        summary = PaperSummary(summary="ok", application="a", prototype="p", impact="i")

        pdf_resp = MagicMock()
        pdf_resp.content = make_pdf(120)  # large enough that truncation changes it

        msg = MagicMock()
        msg.content = [MagicMock(text=summary.model_dump_json())]
        fake_client = MagicMock()
        # First call raises the page-limit error; second (truncated) succeeds.
        fake_client.messages.create.side_effect = [
            FakeBadRequest("document exceeds 100 PDF pages limit"),
            msg,
        ]

        with patch.object(main.time, "sleep"), \
             patch.object(main.requests, "get", return_value=pdf_resp), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            result = process_paper(paper)

        assert result.summary == "ok"
        assert fake_client.messages.create.call_count == 2

    def test_reraises_other_bad_requests(self):
        paper = make_paper(3)
        pdf_resp = MagicMock()
        pdf_resp.content = make_pdf(3)

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = FakeBadRequest("invalid api key")

        with patch.object(main.time, "sleep"), \
             patch.object(main.requests, "get", return_value=pdf_resp), \
             patch.object(main.anthropic, "Anthropic", return_value=fake_client):
            with pytest.raises(anthropic.BadRequestError):
                process_paper(paper)


# --- send_digest ------------------------------------------------------------

class TestSendDigest:
    def test_skips_header_id_and_collects_rest(self):
        # First message is the header (id discarded); subsequent ids collected.
        async def fake_send(text):
            return {"hdr": 1, "p1": 101, "p2": 102}[text]

        with patch.object(main, "send_telegram_message", side_effect=fake_send):
            ids = asyncio.run(send_digest(["hdr", "p1", "p2"]))

        # Header id is dropped; only paper ids returned.
        assert ids == [101, 102]

    def test_handles_failed_send(self):
        async def fake_send(text):
            return None  # every send fails

        with patch.object(main, "send_telegram_message", side_effect=fake_send):
            ids = asyncio.run(send_digest(["hdr", "p1"]))

        # Header dropped; the one paper send failed -> None collected.
        assert ids == [None]


# --- save_paper_to_firestore ------------------------------------------------

class TestSavePaperToFirestore:
    def test_writes_with_message_id_as_doc_id(self):
        fake_db = MagicMock()
        fake_doc = MagicMock()
        fake_db.collection.return_value.document.return_value = fake_doc

        paper = make_paper(3)
        save_paper_to_firestore(paper, telegram_message_id=12345, db=fake_db)

        fake_db.collection.return_value.document.assert_called_once_with("12345")
        fake_doc.set.assert_called_once()
        written = fake_doc.set.call_args[0][0]
        assert written["arxiv_id"] == paper.arxiv_id
        assert written["telegram_message_id"] == 12345
        assert written["score"] == 0
        assert written["last_vote_at"] is None

    def test_constructs_default_client_when_db_none(self):
        fake_db = MagicMock()
        with patch.object(main.firestore, "Client", return_value=fake_db) as ctor:
            save_paper_to_firestore(make_paper(1), telegram_message_id=9, db=None)
        ctor.assert_called_once()
        fake_db.collection.return_value.document.assert_called_once_with("9")