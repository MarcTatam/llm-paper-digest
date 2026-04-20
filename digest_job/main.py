"""
arXiv Daily Digest Service

Fetches new papers from arXiv, ranks them using Claude API,
downloads top 5 PDFs, and sends a digest via Telegram.

Deployed on Cloud Run, triggered daily by Cloud Scheduler.
"""

import base64
import os
import logging
import asyncio
from datetime import datetime
import time

import httpx
import xmltodict
import anthropic
import requests
from google.cloud import firestore
from pydantic import BaseModel, Field, ConfigDict


# --- Configuration ---
ARXIV_CATEGORIES = os.getenv(
    "ARXIV_CATEGORIES", "cs.AI,cs.LG,cs.CL,cs.SE,cs.IR"
).split(",")
ARXIV_MAX_RESULTS = int(os.getenv("ARXIV_MAX_RESULTS", "100"))
TOP_N_PAPERS = int(os.getenv("TOP_N_PAPERS", "5"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL_RANKING = os.getenv("CLAUDE_MODEL_RANKING", "claude-sonnet-4-5-20250514")
CLAUDE_MODEL_SUMMARY = os.getenv("CLAUDE_MODEL_SUMMARY", "claude-sonnet-4-5-20250514")

FIRESTORE_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "sent_papers")

USER_INTERESTS = os.getenv("USER_INTERESTS", """
- LLM-powered tools and applications (RAG, agents, tool use)
- AI engineering and MLOps (deployment, evaluation, monitoring)
- Software engineering practices for AI systems
- Natural language to structured output (NL2SQL, NL2Code)
- Retrieval and information extraction systems
- Practical ML techniques that improve real-world systems
- Scalable system design and architecture
""")

class Paper(BaseModel):
    authors:list[str]
    arxiv_id:str
    title:str
    abstract:str
    categories: list[str] = []

    def get_pdf_url(self):
        return f"https://arxiv.org/pdf/{self.arxiv_id}.pdf"
    
    def get_abs_url(self):
        return f"https://arxiv.org/abs/{self.arxiv_id}"

class PaperSelection(BaseModel):
    selection:list[int] = Field(description="List of indicies of the selected papers.")

    model_config = ConfigDict(extra='forbid')

class PaperSummary(BaseModel):
    summary:str = Field(description="2-3 sentence summary of what this paper does and its key finding. Be direct, no filler.")
    application:str = Field(description="1-2 sentences on where this could be applied in production AI systems.")
    prototype:str = Field(description="1-2 sentences describing a quick prototype using these findings. Assume existing RAG/agent infrastructure.")
    impact:str = Field(description="1 sentence on the practical impact — what changes if this works at scale?")

    model_config = ConfigDict(extra='forbid')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def fetch_arxiv_papers(
    categories: list[str],
    max_results: int = 500,
) -> list[Paper]:
    """Fetch recent papers from arXiv API for given categories."""
    category_query = "+OR+".join(f"cat:{cat.strip()}" for cat in categories)
    url = (
        f"http://export.arxiv.org/api/query?"
        f"search_query={category_query}"
        f"&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={max_results}"
    )

    logger.info(f"Fetching arXiv papers: {url}")

    response = requests.get(url)
    response.raise_for_status()

    parsed = xmltodict.parse(response.text)
    entries = parsed.get("feed", {}).get("entry", [])

    if not entries:
        logger.warning("No papers found from arXiv API")
        return []

    # Ensure entries is always a list (single result comes as dict)
    if isinstance(entries, dict):
        entries = [entries]

    papers = []
    for entry in entries:
        authors_raw = entry.get("author", [])
        if isinstance(authors_raw, dict):
            authors_raw = [authors_raw]
        authors = [a.get("name", "") for a in authors_raw]

        categories_raw = entry.get("category", [])
        if isinstance(categories_raw, dict):
            categories_raw = [categories_raw]
        categories = [c.get("@term", "") for c in categories_raw if c.get("@term")]

        arxiv_id = entry.get("id", "").split("/abs/")[-1]
        abstract:str = entry.get("summary", "").replace("\n", " ").strip()
        if not abstract:
            continue
        title = entry.get("title", "").replace("\n", " ").strip()

        papers.append(Paper(
            arxiv_id=arxiv_id,
            authors=authors,
            title=title,
            abstract=abstract,
            categories=categories
        ))

    logger.info(f"Fetched {len(papers)} papers from arXiv")
    return papers

def rank_papers_with_claude(
    papers: list[Paper],
    top_n: int = 5,
) -> dict:
    """Use Claude to rank papers by relevance and generate a digest."""
    if not papers:
        return {"papers": [], "digest": "No papers found today."}

    # Build paper summaries for the prompt
    paper_list = ""
    for i, p in enumerate(papers):
        authors_str = ", ".join(p.authors[:3])
        if len(p.authors) > 3:
            authors_str += " et al."
        paper_list += (
            f"\n[{i}] {p.title}\n"
            f"    Authors: {authors_str}\n"
            f"    Abstract: {p.abstract}\n"
            f"    ID:{p.arxiv_id}"
        )

    prompt = f"""You are an AI research digest assistant. Your job is to identify the most interesting and relevant papers for a software engineer working in AI/ML consulting.

Here are the user's interests:
{USER_INTERESTS}

Here are today's new arXiv papers:
{paper_list}

Please:
1. Select the top {top_n} most relevant papers based on the user's interests by using their index.
2. Do not select any papers that are purely theoretical or are just benchmarks.
"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    logger.info("Sending papers to Claude for ranking...")
    output_config = anthropic.types.OutputConfigParam(
        format = anthropic.types.JSONOutputFormatParam(
            schema=PaperSelection.model_json_schema(),
            type='json_schema'
        )
    )
    message = client.messages.create(
        model=CLAUDE_MODEL_RANKING,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        output_config=output_config
    )

    selection_response = PaperSelection.model_validate_json(message.content[0].text)
    logger.info("Received ranking from Claude")

    selected_papers = []
    for idx in selection_response.selection[:top_n]:
        if 0 <= idx < len(papers):
            selected_papers.append(papers[idx])

    return selected_papers

def process_paper(paper:Paper)->PaperSummary:
    time.sleep(65)
    reponse = requests.get(paper.get_pdf_url())
    paper = base64.b64encode(reponse.content).decode("utf-8")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    logger.info("Sending paper to Claude for summarisation...")
    output_config = anthropic.types.OutputConfigParam(
        format = anthropic.types.JSONOutputFormatParam(
            schema=PaperSummary.model_json_schema(),
            type='json_schema'
        )
    )
    message = client.messages.create(
        model=CLAUDE_MODEL_SUMMARY,
        max_tokens=2000,
        messages=[{"role": "user", "content": [{
            "type" : "document",
            "source" : {
                "type" : "base64",
                "media_type" : "application/pdf",
                "data" : paper
            }
        },
        {
            "type" : "text",
            "text" : """Summarise this paper for a morning digest read by an AI engineer. Be concise — each field should be a few sentences at most.

Rules:
- No filler phrases like "This paper presents" or "The authors propose" — just say what it does.
- Assume the reader understands transformers, RAG, RL, and standard ML concepts.
- Focus on what's novel, not background.
- For the prototype, be specific and actionable, not vague.
- For impact, think: what changes in production AI systems if this works?
- Total response should be under 250 words."""
        }]}],
        output_config=output_config
    )
    logger.info('Recevieved Claude Summary.')
    summary_response = PaperSummary.model_validate_json(message.content[0].text)
    return summary_response

async def send_telegram_message(text: str, parse_mode: str = "Markdown") -> int | None:
    """Send a single message via Telegram bot, splitting if over 4096 chars."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Telegram credentials not configured")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    chunks = []
    if len(text) > 4000:
        sections = text.split("\n\n")
        current_chunk = ""
        for section in sections:
            if len(current_chunk) + len(section) + 2 > 4000:
                chunks.append(current_chunk)
                current_chunk = section
            else:
                current_chunk += ("\n\n" if current_chunk else "") + section
        if current_chunk:
            chunks.append(current_chunk)
    else:
        chunks = [text]

    first_message_id: int | None = None
    async with httpx.AsyncClient(timeout=15) as client:
        for chunk in chunks:
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            except httpx.HTTPStatusError as e:
                logger.warning(f"Telegram send failed with Markdown, retrying as plain text: {e}")
                payload["parse_mode"] = ""
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
            if first_message_id is None:
                first_message_id = resp.json().get("result", {}).get("message_id")
            await asyncio.sleep(0.5)

    return first_message_id


async def send_digest(messages: list[str]) -> list[int | None]:
    """Send a list of Telegram messages sequentially."""
    message_ids: list[int | None] = []
    for i, message in enumerate(messages):
        msg_id = await send_telegram_message(message)
        if msg_id is None:
            logger.error(f"Failed to send message {i + 1}/{len(messages)}")
        # First message is header, we don't care about it's ID.
        if i > 0:
            message_ids.append(msg_id)
    logger.info(f"Sent digest via Telegram ({len(messages)} message(s))")
    return message_ids

def save_paper_to_firestore(
    paper: Paper,
    telegram_message_id: int,
    db: firestore.Client | None = None,
) -> None:
    """Persist a sent paper to Firestore for later feedback tracking.

    Uses arxiv_id as the document ID so writes are idempotent.
    """
    if db is None:
        db = firestore.Client()

    doc_ref = db.collection(FIRESTORE_COLLECTION).document(paper.arxiv_id)
    doc_ref.set({
        "arxiv_id": paper.arxiv_id,
        "title": paper.title,
        "abstract": paper.abstract,
        "categories": paper.categories,
        "sent_at": firestore.SERVER_TIMESTAMP,
        "telegram_message_id": telegram_message_id,
        "vote_count": 0,
        "last_vote_at": None,
    })
    logger.info(f"Saved paper {paper.arxiv_id} to Firestore (msg_id={telegram_message_id})")


def format_telegram_digest(paper_summaries: list[tuple[Paper, PaperSummary]]) -> list[str]:
    """Format paper summaries into separate Telegram messages — one header + one per paper."""
    date_str = datetime.now().strftime("%A, %d %B %Y")
    header = (
        f"📚 *arXiv Daily Digest*\n"
        f"_{date_str}_\n"
        f"\n"
        f"{len(paper_summaries)} papers selected for you today."
    )

    messages = [header]

    for i, (paper, summary) in enumerate(paper_summaries, 1):
        authors_str = ", ".join(paper.authors[:3])
        if len(paper.authors) > 3:
            authors_str += " et al."

        message = (
            f"*{i}/{len(paper_summaries)}  {_escape_md(paper.title)}*\n"
            f"_{_escape_md(authors_str)}_\n"
            f"\n"
            f"📝 *Summary*\n"
            f"{_escape_md(summary.summary)}\n"
            f"\n"
            f"🔧 *Application*\n"
            f"{_escape_md(summary.application)}\n"
            f"\n"
            f"⚡ *Quick Prototype*\n"
            f"{_escape_md(summary.prototype)}\n"
            f"\n"
            f"💥 *Impact*\n"
            f"{_escape_md(summary.impact)}\n"
            f"\n"
            f"[Read Paper]({paper.get_abs_url()}) · [PDF]({paper.get_pdf_url()})"
        )
        messages.append(message)

    return messages


def _escape_md(text: str) -> str:
    """Escape Telegram Markdown V1 special characters in body text.

    Preserves readability while preventing parse errors. Only escapes
    characters that would break Markdown outside of URLs/formatting
    we control ourselves.
    """
    for char in ("_", "*", "`", "["):
        text = text.replace(char, f"\\{char}")
    return text


def main():
    all_papers = fetch_arxiv_papers(ARXIV_CATEGORIES, ARXIV_MAX_RESULTS)
    top_papers = rank_papers_with_claude(all_papers, TOP_N_PAPERS)
    paper_summaries = [(paper, process_paper(paper)) for paper in top_papers]
    messages = format_telegram_digest(paper_summaries)
    success = asyncio.run(send_digest(messages))
    for i in range(TOP_N_PAPERS):
        save_paper_to_firestore(top_papers[i], messages[i])
    logger.info(f"Digest sent: {success}")

if __name__ == "__main__":
    main()