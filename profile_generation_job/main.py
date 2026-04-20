"""
Profile Generation Job

Runs on a schedule (weekly/monthly via Cloud Scheduler).

Steps:
1. Read all papers from Firestore.
2. Delete papers with no votes past the TTL.
3. Discard interacted papers whose most recent vote predates the last profile update.
4. Regenerate the user profile from the remaining interacted papers + prior profile.
5. Write a new profile document to the `profiles` collection.
"""

import os
import logging
from datetime import datetime, timedelta, timezone

from anthropic import Anthropic
from anthropic.types import OutputConfigParam, JSONOutputFormatParam
from google.cloud import firestore
from pydantic import BaseModel, Field, ConfigDict

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL_PROFILE = os.getenv("CLAUDE_MODEL_PROFILE", "claude-sonnet-4-5-20250514")

PAPERS_COLLECTION = os.getenv("FIRESTORE_COLLECTION", "sent_papers")
PROFILES_COLLECTION = os.getenv("PROFILES_COLLECTION", "profiles")
UNVOTED_TTL_DAYS = int(os.getenv("UNVOTED_TTL_DAYS", "14"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# --- Models ---
class Profile(BaseModel):
    liked_themes: list[str] = Field(
        description="5-10 concrete research themes, techniques, or application areas the user is drawn to."
    )
    disliked_themes: list[str] = Field(
        description="3-8 themes, techniques, or framings the user consistently passes on. Can be empty if no clear signal."
    )
    prose_summary: str = Field(
        description="A 3-5 sentence natural-language description of the user's interests, suitable for dropping into a ranking prompt."
    )

    model_config = ConfigDict(extra="forbid")


class InteractedPaper(BaseModel):
    """A paper pulled from Firestore that has been voted on (up or down)."""
    arxiv_id: str
    title: str
    abstract: str
    categories: list[str]
    vote_count: int
    last_vote_at: datetime


# --- Firestore helpers ---
def _get_last_profile_timestamp(db: firestore.Client) -> datetime | None:
    """Return the generated_at of the most recent profile, or None if no profile exists yet."""
    docs = (
        db.collection(PROFILES_COLLECTION)
        .order_by("generated_at", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        return data.get("generated_at")
    return None


def _get_last_profile(db: firestore.Client) -> Profile | None:
    """Return the most recent profile as a Profile model, or None."""
    docs = (
        db.collection(PROFILES_COLLECTION)
        .order_by("generated_at", direction=firestore.Query.DESCENDING)
        .limit(1)
        .stream()
    )
    for doc in docs:
        data = doc.to_dict()
        try:
            return Profile(
                liked_themes=data.get("liked_themes", []),
                disliked_themes=data.get("disliked_themes", []),
                prose_summary=data.get("prose_summary", ""),
            )
        except Exception as e:
            logger.warning(f"Failed to parse prior profile: {e}")
            return None
    return None


def prune_expired_papers(db: firestore.Client, ttl_days: int) -> int:
    """Delete papers with no votes whose sent_at is older than ttl_days. Returns count deleted."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)

    # "Never voted on" = last_vote_at is None. Firestore can query for null directly.
    never_voted = (
        db.collection(PAPERS_COLLECTION)
        .where("last_vote_at", "==", None)
        .stream()
    )

    deleted = 0
    batch = db.batch()
    batch_size = 0
    for doc in never_voted:
        sent_at = doc.to_dict().get("sent_at")
        if sent_at is None or sent_at >= cutoff:
            continue
        batch.delete(doc.reference)
        batch_size += 1
        deleted += 1
        # Firestore batch limit is 500 writes
        if batch_size >= 400:
            batch.commit()
            batch = db.batch()
            batch_size = 0

    if batch_size > 0:
        batch.commit()

    logger.info(f"Pruned {deleted} papers with no votes (older than {ttl_days}d)")
    return deleted


def fetch_interacted_papers(
    db: firestore.Client,
    since: datetime | None,
) -> list[InteractedPaper]:
    """Fetch papers with any vote activity, optionally filtering by last_vote_at > since."""
    query = db.collection(PAPERS_COLLECTION).where("last_vote_at", "!=", None)
    docs = query.stream()

    papers: list[InteractedPaper] = []
    for doc in docs:
        data = doc.to_dict()
        last_vote_at = data["last_vote_at"]
        if since is not None and last_vote_at <= since:
            continue
        try:
            papers.append(InteractedPaper(
                arxiv_id=data["arxiv_id"],
                title=data["title"],
                abstract=data["abstract"],
                categories=data.get("categories", []),
                vote_count=data["vote_count"],
                last_vote_at=last_vote_at,
            ))
        except KeyError as e:
            logger.warning(f"Skipping malformed doc {doc.id}: missing {e}")

    logger.info(
        f"Found {len(papers)} interacted papers"
        + (f" since {since.isoformat()}" if since else " (no prior profile)")
    )
    return papers


# --- Profile generation ---
def generate_profile(
    interacted_papers: list[InteractedPaper],
    prior_profile: Profile | None,
) -> Profile:
    """Ask Claude to generate/update the user profile based on interacted papers."""
    # Sort by |vote_count| desc so the strongest signal is first, regardless of direction.
    interacted_papers = sorted(interacted_papers, key=lambda p: abs(p.vote_count), reverse=True)

    paper_block = ""
    for p in interacted_papers:
        paper_block += (
            f"\n- [{p.vote_count:+d} votes] {p.title}\n"
            f"  Categories: {', '.join(p.categories) or 'n/a'}\n"
            f"  Abstract: {p.abstract}\n"
        )

    prior_block = ""
    if prior_profile is not None:
        prior_block = f"""
Prior profile (for context — update it, don't just restate it):
Liked themes: {', '.join(prior_profile.liked_themes)}
Disliked themes: {', '.join(prior_profile.disliked_themes)}
Summary: {prior_profile.prose_summary}
"""

    prompt = f"""You are generating an interest profile for an AI/ML engineer based on their recent paper votes.

Positive vote counts mean the user found the paper valuable. Negative counts mean they explicitly passed.
{prior_block}
Recent interacted papers (sorted by signal strength):
{paper_block}

Generate a profile that captures what this user actually cares about, based on evidence from the votes. Be specific — "LLM evaluation pipelines" is better than "AI". If the prior profile is present, treat it as a prior belief and update it in light of the new evidence; don't wholesale replace it unless the new signal clearly contradicts it.

Avoid generic phrasing. The profile will be pasted into a future ranking prompt, so concreteness helps the model discriminate."""

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    output_config = OutputConfigParam(
        format=JSONOutputFormatParam(
            schema=Profile.model_json_schema(),
            type="json_schema",
        )
    )

    logger.info(f"Generating profile from {len(interacted_papers)} interacted papers...")
    message = client.messages.create(
        model=CLAUDE_MODEL_PROFILE,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
        output_config=output_config,
    )
    return Profile.model_validate_json(message.content[0].text)


def save_profile(
    db: firestore.Client,
    profile: Profile,
    source_paper_ids: list[str],
) -> str:
    """Write a new profile doc. Returns the new doc ID."""
    now = datetime.now(timezone.utc)
    doc_id = now.strftime("%Y%m%dT%H%M%SZ")
    doc_ref = db.collection(PROFILES_COLLECTION).document(doc_id)
    doc_ref.set({
        "generated_at": firestore.SERVER_TIMESTAMP,
        "liked_themes": profile.liked_themes,
        "disliked_themes": profile.disliked_themes,
        "prose_summary": profile.prose_summary,
        "source_paper_ids": source_paper_ids,
        "source_paper_count": len(source_paper_ids),
    })
    logger.info(f"Saved profile {doc_id} ({len(source_paper_ids)} source papers)")
    return doc_id


# --- Entrypoint ---
def main():
    db = firestore.Client()

    # 1. Prune expired papers with no votes.
    prune_expired_papers(db, UNVOTED_TTL_DAYS)

    # 2. Find the last profile's timestamp (the "since" cutoff).
    last_profile_ts = _get_last_profile_timestamp(db)
    prior_profile = _get_last_profile(db) if last_profile_ts else None

    # 3. Fetch interacted papers with new votes since the last profile.
    interacted_papers = fetch_interacted_papers(db, since=last_profile_ts)

    if not interacted_papers:
        # This should never be the case since the webhook service triggers this job.
        logger.info("No new voting signal since last profile — skipping regeneration")
        return

    # 4. Regenerate the profile.
    profile = generate_profile(interacted_papers, prior_profile=prior_profile)

    # 5. Persist.
    save_profile(
        db,
        profile,
        source_paper_ids=[p.arxiv_id for p in interacted_papers],
    )


if __name__ == "__main__":
    main()