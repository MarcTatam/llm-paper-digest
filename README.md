# 📚 LLM Paper Digest

An automated daily pipeline that fetches new arXiv papers, uses Claude to rank them by relevance to your interests, summarises the top papers from their full PDFs, and delivers a digest to Telegram.

Runs as a Cloud Run Job on GCP, triggered by Cloud Scheduler.

## Architecture

```
Cloud Scheduler (8am Mon–Fri)
    → Cloud Run Job
        → arXiv API — fetch latest papers (titles + abstracts)
        → Claude (Sonnet) — rank top 5 by relevance
        → arXiv PDF download — fetch full papers
        → Claude (Haiku) — summarise each paper from the PDF
        → Telegram Bot API — send formatted digest
```

## What the Digest Looks Like

Each morning you get a Telegram message with 5 papers, each containing:

- **Summary** — core concepts and key findings
- **Application** — where this research could be applied
- **Quick Prototype** — a concrete thing you could build with it
- **Benefits** — why this paper matters
- **Links** — direct links to the abstract and PDF

## Setup

### Prerequisites

- A GCP project with billing enabled
- [Anthropic API key](https://console.anthropic.com) (Sonnet for ranking, Haiku for summaries — costs ~£1–2/month)
- A Telegram bot (create one via [@BotFather](https://t.me/BotFather))

### 1. Create the Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`
2. Save the bot token
3. Create a channel and add the bot as an admin with "Post Messages" permission
4. Get the chat ID — either use `@your_channel_name` or send a message and visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find the numeric ID

### 2. Store Secrets in GCP Secret Manager

```bash
echo -n "sk-ant-..." | gcloud secrets create anthropic-api-key --data-file=-
echo -n "7123456..." | gcloud secrets create bot_key --data-file=-
echo -n "@your_channel" | gcloud secrets create telegram-chat-id --data-file=-
```

### 3. Enable APIs

```bash
gcloud services enable \
    cloudbuild.googleapis.com \
    run.googleapis.com \
    containerregistry.googleapis.com \
    secretmanager.googleapis.com \
    cloudscheduler.googleapis.com
```

### 4. Build and Deploy

```bash
gcloud builds submit --project=YOUR_PROJECT_ID
```

This uses `cloudbuild.yaml` to build the Docker image, push it to Container Registry, and deploy as a Cloud Run Job.

### 5. Schedule the Job

```bash
gcloud scheduler jobs create http arxiv-daily-digest \
    --location=europe-west2 \
    --schedule="0 8 * * 1-5" \
    --time-zone="Europe/London" \
    --uri="https://europe-west2-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/YOUR_PROJECT_ID/jobs/arxiv-digest:run" \
    --http-method=POST \
    --oauth-service-account-email=YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com \
    --attempt-deadline=300s
```

### 6. Test It

```bash
gcloud run jobs execute arxiv-digest --region=europe-west2
```

## Configuration

All configuration is via environment variables, set in `cloudbuild.yaml`:

| Variable | Default | Description |
|---|---|---|
| `ARXIV_CATEGORIES` | `cs.AI,cs.LG,cs.CL,cs.SE,cs.IR` | arXiv categories to fetch |
| `ARXIV_MAX_RESULTS` | `500` | Number of papers to fetch from arXiv |
| `TOP_N_PAPERS` | `5` | Number of papers to include in the digest |
| `CLAUDE_MODEL_RANKING` | `claude-sonnet-4-6` | Model used for ranking papers |
| `CLAUDE_MODEL_SUMMARY` | `claude-haiku-4-5-20251001` | Model used for PDF summarisation |
| `USER_INTERESTS` | See `main.py` | Your interest profile for ranking |

### Customising Your Interests

Edit the `USER_INTERESTS` variable in `main.py` to tune what papers Claude selects. The more specific you are, the better the ranking.

## Project Structure

```
├── app/
│   └── main.py              # Pipeline: fetch → rank → summarise → send
├── Dockerfile
├── pyproject.toml            # Dependencies managed via uv
├── uv.lock
├── cloudbuild.yaml           # CI/CD: build, push, deploy to Cloud Run
└── README.md
```

## Design Decisions

**Two-model approach** — Sonnet handles ranking (needs stronger reasoning over many abstracts) while Haiku handles per-paper summarisation (simpler task, cheaper, and runs 5x). This keeps costs low without sacrificing ranking quality.

**arXiv API over email parsing** — The arXiv API returns structured data (titles, abstracts, IDs) directly, avoiding the fragility of parsing email HTML that could change format at any time.

**Cloud Run Job over Service** — This is a run-to-completion task, not a long-running server. Jobs are the right primitive: no health checks, no idle instances, no HTTP endpoint to secure.

**Rate limiting on PDF processing** — A 65-second delay between PDF summarisation calls avoids hitting arXiv's rate limits and API token limits.

## Costs

| Component | Cost |
|---|---|
| Cloud Run | Free tier (2M requests/month) |
| Cloud Scheduler | Free (3 jobs/month) |
| Claude API (ranking, Sonnet) | ~$0.02/day |
| Claude API (summaries, Haiku) | ~$0.20/day (~200k tokens across 5 PDFs) |
| arXiv API | Free, no key required |
| Telegram API | Free |
| **Total** | **~$5–7/month** |

## License

MIT