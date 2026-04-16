# 📚 LLM Paper Digest

I wanted to stay current with ML research without spending an hour a day triaging arXiv. Existing tools either sent me too much (daily category digests with 200 papers) or too little (curated newsletters that don't match my specific interests). This is the compromise: five papers a day, ranked against my own interest profile, summarised from the full PDF rather than just the abstract.

Runs as a Cloud Run Job on GCP, triggered by Cloud Scheduler. Infrastructure managed with Terraform.

## Architecture

```
Cloud Scheduler (8am Mon–Fri, Europe/London)
    → Cloud Run Job
        → arXiv API — fetch latest papers (titles + abstracts)
        → Claude — rank top 5 by relevance against user interest profile
        → arXiv PDF download — fetch full papers
        → Claude — summarise each paper from the PDF (structured output)
        → Telegram Bot API — send formatted digest (one message per paper)
```

The ranking and summarisation steps can use different Claude models — Sonnet for ranking (stronger reasoning over many abstracts), Haiku for summarisation (simpler per-paper task, cheaper at 5× the calls). Defaults in `cloudbuild.yaml` reflect this split; the code itself defaults both to Sonnet so local runs work without extra config.

## What the Digest Looks Like

Each morning you get a Telegram message per paper, each containing:

* **Summary** — core concepts and key findings
* **Application** — where this research could be applied
* **Quick Prototype** — a concrete thing you could build with it
* **Impact** — what changes if this works at scale
* **Links** — direct links to the abstract and PDF

[![Sample Telegram digest showing a paper summary](docs/sample-image.png)](docs/sample-image.png)

## Setup

### Prerequisites

* A GCP project with billing enabled
* [Anthropic API key](https://console.anthropic.com) — costs ~£5–7/month at the default config
* A Telegram bot (create one via [@BotFather](https://t.me/BotFather))
* [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
* [gcloud CLI](https://cloud.google.com/sdk/docs/install) authenticated to your project
* A GCS bucket for Terraform state (referenced in `main.tf`'s `backend "gcs"` block)

### 1. Create the Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram and send `/newbot`
2. Save the bot token
3. Create a channel and add the bot as an admin with "Post Messages" permission
4. Get the chat ID — either use `@your_channel_name` or send a message to the channel and visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find the numeric ID

### 2. Deploy Infrastructure with Terraform

The Terraform config creates the Secret Manager *shells* but not their values — populate those separately after `apply`.

```bash
cd infrastructure

# Create terraform.tfvars with your values
cat > terraform.tfvars <<EOF
project_name         = "your-gcp-project-id"
project_number       = "123456789012"
region               = "europe-west2"
arxiv_categories     = "cs.AI,cs.LG,cs.CL,cs.SE,cs.IR"
arxiv_max_results    = 300
top_n_papers         = 5
claude_model_ranking = "claude-sonnet-4-6"
claude_model_summary = "claude-haiku-4-5-20251001"
image                = "gcr.io/your-gcp-project-id/arxiv-digest:latest"
EOF

# Configure the GCS backend bucket (edit main.tf or use -backend-config)
terraform init -backend-config="bucket=your-tfstate-bucket"
terraform plan
terraform apply
```

This provisions all GCP resources: APIs, service accounts, IAM bindings, Secret Manager secrets, the Cloud Run Job, and the Cloud Scheduler trigger.

### 3. Populate Secrets

```bash
echo -n "sk-ant-..."     | gcloud secrets versions add anthropic-api-key --data-file=-
echo -n "7123456:ABC..." | gcloud secrets versions add bot_key --data-file=-
echo -n "@your_channel"  | gcloud secrets versions add telegram-chat-id --data-file=-
```

### 4. Build and Deploy the Container

```bash
cd job
gcloud builds submit --project=YOUR_PROJECT_ID
```

`cloudbuild.yaml` builds the Docker image, pushes it to Container Registry, and redeploys the Cloud Run Job with environment variables and secret mounts.

### 5. Test It

```bash
gcloud run jobs execute arxiv-digest --region=europe-west2
```

## Project Structure

```
├── job/
│   ├── main.py                    # Pipeline: fetch → rank → summarise → send
│   ├── Dockerfile                 # python:3.12-slim + uv
│   ├── pyproject.toml             # Dependencies (uv-managed)
│   ├── uv.lock
│   └── cloudbuild.yaml            # CI/CD: build, push, deploy job
├── infrastructure/
│   ├── main.tf                    # All GCP resources (APIs, SAs, secrets, job, scheduler)
│   ├── providers.tf               # Google provider config
│   └── variables.tf               # Input variables
├── docs/
│   └── sample-image.png           # Sample digest screenshot
└── README.md
```

## Infrastructure

All GCP resources are managed via Terraform in `infrastructure/`. State lives in GCS (configured via the `backend "gcs"` block — you'll need to supply the bucket name at init time).

### Resources Managed

| Resource | Purpose |
|---|---|
| `google_project_service` | Enables required GCP APIs (Cloud Run, Cloud Build, Cloud Scheduler, Secret Manager, Container Registry) |
| `google_service_account` (runner) | Dedicated SA for the Cloud Run Job — reads secrets at runtime |
| `google_service_account` (scheduler) | Dedicated SA for Cloud Scheduler — invokes the job |
| `google_secret_manager_secret` × 3 | Creates secret shells for the Anthropic API key, Telegram bot token, and chat ID |
| `google_secret_manager_secret_iam_member` × 3 | Grants the runner SA `secretAccessor` on each secret |
| `google_cloud_run_v2_job` | The digest job — container config, env vars, secret-backed env vars, 600s timeout, 512Mi memory |
| `google_cloud_run_v2_job_iam_member` | Grants the scheduler SA `run.invoker` on the job |
| `google_cloud_scheduler_job` | Cron trigger — `0 8 * * 1-5` in `Europe/London` |

Secret *values* are intentionally not in Terraform state — they're added via `gcloud secrets versions add` after the shells are created.

### Terraform Variables

All variables are required (no defaults set in `variables.tf` except `top_n_papers`):

| Variable | Example | Description |
|---|---|---|
| `project_name` | `my-gcp-project` | GCP project ID |
| `project_number` | `123456789012` | GCP project number (used in the Scheduler target URI) |
| `region` | `europe-west2` | GCP region for all resources |
| `arxiv_categories` | `cs.AI,cs.LG,cs.CL,cs.SE,cs.IR` | arXiv categories to track |
| `arxiv_max_results` | `300` | Papers to fetch per run |
| `top_n_papers` | `5` | Papers to include in the digest (default: 5) |
| `claude_model_ranking` | `claude-sonnet-4-6` | Model for ranking |
| `claude_model_summary` | `claude-haiku-4-5-20251001` | Model for summarisation |
| `image` | `gcr.io/PROJECT/arxiv-digest:latest` | Container image URI |

## Runtime Configuration

Environment variables set on the Cloud Run Job. Terraform manages the non-secret ones; `cloudbuild.yaml` overrides them on each deploy.

| Variable | Code default | Description |
|---|---|---|
| `ARXIV_CATEGORIES` | `cs.AI,cs.LG,cs.CL,cs.SE,cs.IR` | arXiv categories to fetch |
| `ARXIV_MAX_RESULTS` | `100` | Number of papers to fetch from arXiv |
| `TOP_N_PAPERS` | `5` | Number of papers to include in the digest |
| `CLAUDE_MODEL_RANKING` | `claude-sonnet-4-5-20250514` | Model used for ranking papers |
| `CLAUDE_MODEL_SUMMARY` | `claude-sonnet-4-5-20250514` | Model used for PDF summarisation |
| `USER_INTERESTS` | See `main.py` | Your interest profile for ranking (multi-line string) |
| `ANTHROPIC_API_KEY` | — | Injected from Secret Manager |
| `TELEGRAM_BOT_TOKEN` | — | Injected from Secret Manager |
| `TELEGRAM_CHAT_ID` | — | Injected from Secret Manager |

### Customising Your Interests

Edit the `USER_INTERESTS` variable in `job/main.py` (or override via env var) to tune what papers Claude selects. The more specific you are, the better the ranking — it's prepended verbatim into the ranking prompt.

## Design Decisions

**Two-model approach** — Sonnet handles ranking (stronger reasoning across ~300 abstracts in one prompt), Haiku handles per-paper summarisation (simpler task, cheaper, and runs 5× per day). This keeps costs low without sacrificing ranking quality. The split is applied via env vars in `cloudbuild.yaml`; the code defaults both to Sonnet so local runs work without further config.

**Structured output over prompt-and-parse** — Ranking and summarisation both use Anthropic's JSON schema output, validated by Pydantic (`PaperSelection`, `PaperSummary`). No regex, no retry-on-bad-JSON.

**arXiv API over email parsing** — The arXiv API returns structured data (titles, abstracts, IDs) directly, avoiding the fragility of parsing email HTML that could change format at any time.

**Cloud Run Job over Service** — This is a run-to-completion task, not a long-running server. Jobs are the right primitive: no health checks, no idle instances, no HTTP endpoint to secure.

**Dedicated service accounts** — Two separate SAs with minimal permissions: the runner (secret access only), the scheduler (job invocation only). No use of the default compute SA.

**Terraform for infrastructure** — All GCP resources are declarative and reproducible. Secret *values* are the only manual step, kept outside Terraform to avoid sensitive data in state files.

**Rate limiting on PDF processing** — A 65-second sleep before each PDF summarisation call stays well inside arXiv's rate limits and avoids hitting Anthropic's per-minute token limits on larger PDFs.

**One Telegram message per paper** — Easier to skim on mobile, each paper gets its own notification, and the code naturally handles the 4096-char limit by treating each paper as its own message (with a separate header message).

## Costs

| Component | Cost |
|---|---|
| Cloud Run | Free tier (2M requests/month) |
| Cloud Scheduler | Free (3 jobs/month) |
| Claude API (ranking, Sonnet) | ~£0.02/day |
| Claude API (summaries, Haiku) | ~£0.20/day (~200k tokens across 5 PDFs) |
| Secret Manager | Negligible (<£0.10/month) |
| arXiv API | Free, no key required |
| Telegram API | Free |
| **Total** | **~£5–7/month** |

## License

MIT