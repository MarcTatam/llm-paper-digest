"""Tests for the profile generation job.

Run from the profile_generation_job directory:  pytest test_main.py
or from repo root:                              pytest profile_generation_job/test_main.py

Firestore is replaced with lightweight fakes; Claude is mocked. The focus is
the pruning logic, the interacted-paper filtering, profile parsing, and the
ordering applied before the prompt is built.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import main
from main import (
    InteractedPaper,
    Profile,
    _get_last_profile,
    _get_last_profile_timestamp,
    fetch_interacted_papers,
    generate_profile,
    prune_expired_papers,
    save_profile,
)


# --- fake Firestore primitives ----------------------------------------------

class FakeDoc:
    def __init__(self, doc_id, data):
        self.id = doc_id
        self._data = data
        self.reference = MagicMock(name=f"ref:{doc_id}")

    def to_dict(self):
        return self._data


class FakeQuery:
    """Supports the chained calls used in the module: where / order_by / limit / stream."""

    def __init__(self, docs):
        self._docs = docs

    def where(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def stream(self):
        return iter(self._docs)


class FakeBatch:
    def __init__(self, sink):
        self._sink = sink

    def delete(self, ref):
        self._sink.append(ref)

    def commit(self):
        pass


class FakeCollection:
    def __init__(self, docs, deleted_sink, written_sink, collection_name):
        self._docs = docs
        self._deleted_sink = deleted_sink
        self._written_sink = written_sink
        self._name = collection_name

    def where(self, *args, **kwargs):
        return FakeQuery(self._docs)

    def order_by(self, *args, **kwargs):
        return FakeQuery(self._docs)

    def document(self, doc_id):
        doc_ref = MagicMock()

        def _set(payload):
            self._written_sink.append((self._name, doc_id, payload))

        doc_ref.set.side_effect = _set
        return doc_ref


class FakeDB:
    """Routes collection() calls to per-collection doc sets."""

    def __init__(self, collections: dict):
        # collections: {name: [FakeDoc, ...]}
        self._collections = collections
        self.deleted = []
        self.written = []

    def collection(self, name):
        docs = self._collections.get(name, [])
        return FakeCollection(docs, self.deleted, self.written, name)

    def batch(self):
        return FakeBatch(self.deleted)


# --- prune_expired_papers ---------------------------------------------------

class TestPruneExpiredPapers:
    def test_deletes_old_unvoted_papers(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=30)
        recent = now - timedelta(days=2)

        docs = [
            FakeDoc("old1", {"last_vote_at": None, "sent_at": old}),
            FakeDoc("old2", {"last_vote_at": None, "sent_at": old}),
            FakeDoc("recent", {"last_vote_at": None, "sent_at": recent}),
        ]
        db = FakeDB({main.PAPERS_COLLECTION: docs})

        deleted = prune_expired_papers(db, ttl_days=14)

        assert deleted == 2
        assert len(db.deleted) == 2

    def test_keeps_recent_unvoted_papers(self):
        now = datetime.now(timezone.utc)
        docs = [
            FakeDoc("r1", {"last_vote_at": None, "sent_at": now - timedelta(days=1)}),
        ]
        db = FakeDB({main.PAPERS_COLLECTION: docs})

        deleted = prune_expired_papers(db, ttl_days=14)

        assert deleted == 0
        assert db.deleted == []

    def test_skips_docs_with_missing_sent_at(self):
        docs = [FakeDoc("no_sent", {"last_vote_at": None, "sent_at": None})]
        db = FakeDB({main.PAPERS_COLLECTION: docs})

        deleted = prune_expired_papers(db, ttl_days=14)

        assert deleted == 0

    def test_batches_large_deletions(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=60)
        # 850 docs -> spans more than one 400-write batch.
        docs = [
            FakeDoc(f"d{i}", {"last_vote_at": None, "sent_at": old})
            for i in range(850)
        ]
        db = FakeDB({main.PAPERS_COLLECTION: docs})

        deleted = prune_expired_papers(db, ttl_days=14)

        assert deleted == 850
        assert len(db.deleted) == 850


# --- fetch_interacted_papers ------------------------------------------------

class TestFetchInteractedPapers:
    def _paper_doc(self, doc_id, score, last_vote_at, **overrides):
        data = {
            "arxiv_id": f"arxiv-{doc_id}",
            "title": f"Title {doc_id}",
            "abstract": f"Abstract {doc_id}",
            "categories": ["cs.AI"],
            "score": score,
            "last_vote_at": last_vote_at,
        }
        data.update(overrides)
        return FakeDoc(doc_id, data)

    def test_no_prior_profile_returns_all_voted(self):
        # since=None path: query filters score != 0; fake returns whatever docs exist.
        docs = [
            self._paper_doc("a", 3, datetime.now(timezone.utc)),
            self._paper_doc("b", -2, datetime.now(timezone.utc)),
        ]
        db = FakeDB({main.PAPERS_COLLECTION: docs})

        papers = fetch_interacted_papers(db, since=None)

        assert len(papers) == 2
        assert {p.arxiv_id for p in papers} == {"arxiv-a", "arxiv-b"}

    def test_since_filter_excludes_old_votes(self):
        since = datetime(2024, 6, 1, tzinfo=timezone.utc)
        docs = [
            self._paper_doc("new", 1, datetime(2024, 7, 1, tzinfo=timezone.utc)),
            self._paper_doc("equal", 1, since),  # <= since must be excluded
            self._paper_doc("old", 1, datetime(2024, 5, 1, tzinfo=timezone.utc)),
        ]
        db = FakeDB({main.PAPERS_COLLECTION: docs})

        papers = fetch_interacted_papers(db, since=since)

        # Only the strictly-after doc survives the in-code guard.
        assert [p.arxiv_id for p in papers] == ["arxiv-new"]

    def test_malformed_doc_skipped(self):
        good = self._paper_doc("good", 2, datetime.now(timezone.utc))
        # Missing "title" -> KeyError -> skipped.
        bad = FakeDoc("bad", {
            "arxiv_id": "arxiv-bad",
            "abstract": "x",
            "score": 1,
            "last_vote_at": datetime.now(timezone.utc),
        })
        db = FakeDB({main.PAPERS_COLLECTION: [good, bad]})

        papers = fetch_interacted_papers(db, since=None)

        assert [p.arxiv_id for p in papers] == ["arxiv-good"]


# --- generate_profile -------------------------------------------------------

class TestGenerateProfile:
    def _make_interacted(self, score):
        return InteractedPaper(
            arxiv_id=f"id-{score}",
            title=f"t{score}",
            abstract="a",
            categories=["cs.AI"],
            score=score,
            last_vote_at=datetime.now(timezone.utc),
        )

    def test_sorts_by_absolute_score_in_prompt(self):
        papers = [
            self._make_interacted(2),
            self._make_interacted(-9),
            self._make_interacted(4),
        ]
        profile = Profile(liked_themes=["x"], disliked_themes=[], prose_summary="s")

        captured = {}

        def fake_create(*args, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            msg = MagicMock()
            msg.content = [MagicMock(text=profile.model_dump_json())]
            return msg

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = fake_create

        with patch.object(main, "Anthropic", return_value=fake_client):
            generate_profile(papers, prior_profile=None)

        prompt = captured["prompt"]
        # The prompt lists papers by title (t<score>) with the strongest
        # absolute signal first: |−9| > |4| > |2|.
        assert prompt.index("t-9") < prompt.index("t4") < prompt.index("t2")

    def test_prior_profile_included_in_prompt(self):
        papers = [self._make_interacted(1)]
        prior = Profile(
            liked_themes=["RAG pipelines"],
            disliked_themes=["pure theory"],
            prose_summary="Prior summary text.",
        )
        result = Profile(liked_themes=["y"], disliked_themes=[], prose_summary="s")

        captured = {}

        def fake_create(*args, **kwargs):
            captured["prompt"] = kwargs["messages"][0]["content"]
            msg = MagicMock()
            msg.content = [MagicMock(text=result.model_dump_json())]
            return msg

        fake_client = MagicMock()
        fake_client.messages.create.side_effect = fake_create

        with patch.object(main, "Anthropic", return_value=fake_client):
            generate_profile(papers, prior_profile=prior)

        assert "RAG pipelines" in captured["prompt"]
        assert "Prior summary text." in captured["prompt"]

    def test_returns_parsed_profile(self):
        papers = [self._make_interacted(1)]
        result = Profile(
            liked_themes=["agents", "evals"],
            disliked_themes=["benchmarks"],
            prose_summary="Likes practical AI engineering.",
        )

        msg = MagicMock()
        msg.content = [MagicMock(text=result.model_dump_json())]
        fake_client = MagicMock()
        fake_client.messages.create.return_value = msg

        with patch.object(main, "Anthropic", return_value=fake_client):
            out = generate_profile(papers, prior_profile=None)

        assert out.liked_themes == ["agents", "evals"]
        assert out.disliked_themes == ["benchmarks"]


# --- save_profile -----------------------------------------------------------

class TestSaveProfile:
    def test_writes_profile_doc(self):
        db = FakeDB({})
        profile = Profile(
            liked_themes=["a"], disliked_themes=["b"], prose_summary="s",
        )

        doc_id = save_profile(db, profile, source_paper_ids=["p1", "p2", "p3"])

        assert len(db.written) == 1
        collection, written_id, payload = db.written[0]
        assert collection == main.PROFILES_COLLECTION
        assert written_id == doc_id
        assert payload["liked_themes"] == ["a"]
        assert payload["disliked_themes"] == ["b"]
        assert payload["source_paper_count"] == 3
        assert payload["source_paper_ids"] == ["p1", "p2", "p3"]


# --- profile fetch helpers --------------------------------------------------

class TestProfileFetchHelpers:
    def test_last_profile_timestamp_none_when_empty(self):
        db = FakeDB({main.PROFILES_COLLECTION: []})
        assert _get_last_profile_timestamp(db) is None

    def test_last_profile_timestamp_returned(self):
        ts = datetime(2024, 7, 1, tzinfo=timezone.utc)
        db = FakeDB({main.PROFILES_COLLECTION: [FakeDoc("p", {"generated_at": ts})]})
        assert _get_last_profile_timestamp(db) == ts

    def test_last_profile_parsed(self):
        db = FakeDB({main.PROFILES_COLLECTION: [FakeDoc("p", {
            "liked_themes": ["x"],
            "disliked_themes": ["y"],
            "prose_summary": "z",
            "generated_at": datetime.now(timezone.utc),
        })]})
        profile = _get_last_profile(db)
        assert profile is not None
        assert profile.liked_themes == ["x"]
        assert profile.prose_summary == "z"

    def test_last_profile_none_when_empty(self):
        db = FakeDB({main.PROFILES_COLLECTION: []})
        assert _get_last_profile(db) is None

    def test_last_profile_none_on_parse_failure(self):
        # liked_themes as a non-list makes Profile validation raise; the helper
        # catches it and returns None rather than propagating.
        db = FakeDB({main.PROFILES_COLLECTION: [FakeDoc("p", {
            "liked_themes": "not-a-list",
            "disliked_themes": 123,
            "prose_summary": None,
            "generated_at": datetime.now(timezone.utc),
        })]})
        assert _get_last_profile(db) is None