# 📚 LLM Paper Digest

I wanted to stay current with ML research without spending an hour a day triaging arXiv. Existing tools either sent me too much (daily category digests with 200 papers) or too little (curated newsletters that don't match my specific interests). This is the compromise: five papers a day, ranked against my own evolving interest profile, summarised from the full PDF rather than just the abstract — and a feedback loop that learns from my Telegram reactions so the ranking gets sharper over time.

Runs on GCP. Three services, all Terraform-managed.

## Architecture

**Daily digest** (Mon–Fri, 08:00 Europe/London)

```
Cloud Scheduler
    → Cloud Run Job: digest_job (Python)
        → arXiv API — fetch latest papers (titles + abstracts)
        → Firestore — load latest user profile
        → Claude — rank top N papers against profile
        → arXiv PDF download — fetch full papers
        → Claude — summarise each paper from the PDF (structured output)
        → Telegram Bot API — send formatted digest (one message per paper)
        → Firestore — record sent papers (msg_id, arxiv_id, score=0)
```

**On reaction**

```
Telegram (message_reaction_count update)
    → Cloud Run Service: webhook_service (Go)
        → validate X-Telegram-Bot-Api-Secret-Token header
        → Firestore — update score & last_vote_at on the paper doc
        → if updated-paper count since last profile ≥ threshold:
            → Cloud Tasks — enqueue a profile-regen task (OIDC auth)
```

**Profile regeneration**

```
Cloud Tasks (concurrency=1, exponential backoff)
    → Cloud Run Job: profile_generation_job (Python)
        → Firestore — prune unvoted papers older than TTL
        → Firestore — fetch papers voted on since last profile
        → Claude — generate updated profile (liked/disliked themes + prose)
        → Firestore — write new profile doc
```

The next morning's digest reads the latest profile from Firestore and uses it as the ranking prompt. Over time, the ranker learns from your votes without you having to edit a config.

## What the Digest Looks Like

Each morning, one Telegram message per paper, each containing:

* **Summary** — core concepts and key findings
* **Application** — where this research could be applied
* **Quick Prototype** — a concrete thing you could build with it
* **Impact** — what changes if this works at scale
* **Links** — direct links to the abstract and PDF
* **Reactions** — react with 👍 or 👎 to feed the profile loop

[![Sample Telegram digest showing a paper summary](docs/sample-image.png)](docs/sample-image.png)

## Repository Layout

```
├── digest_job/                # Daily ranking + summary + send (Python, Cloud Run Job)
│   ├── main.py
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   └── cloudbuild.yaml
├── webhook_service/           # Telegram reaction receiver (Go, Cloud Run Service)
│   ├── webhook.go
│   ├── go.mod / go.sum
│   ├── Dockerfile
│   └── cloudbuild.yaml
├── profile_generation_job/    # Profile regeneration (Python, Cloud Run Job)
│   ├── main.py
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── uv.lock
│   └── cloudbuild.yaml
├── infrastructure/            # All GCP resources (Terraform)
│   ├── main.tf                # APIs + shared SAs
│   ├── providers.tf
│   ├── variables.tf
│   ├── secrets.tf             # Secret Manager shells
│   ├── firestore.tf           # (default) database
│   ├── digest_job.tf          # Daily Cloud Run Job
│   ├── digest_scheduler.tf    # Cloud Scheduler trigger
│   ├── webhook_service.tf     # Cloud Run Service for Telegram webhooks
│   ├── profile_job.tf         # Profile-regen Cloud Run Job
│   └── profile_regen_queue.tf # Cloud Tasks queue + invoker SA
├── docs/
│   └── sample-image.png
└── README.md
```

## Firestore Schema

Two collections in the `(default)` database.

**`sent_papers`** — one doc per paper sent. Doc ID is the Telegram message ID, so the webhook can look up the target paper directly from the reaction event.

| Field | Type | Notes |
|---|---|---|
| `telegram_message_id` | int | The Telegram message ID |
| `arxiv_id` | string | e.g. `2510.12345` |
| `title` | string | |
| `abstract` | string | |
| `categories` | list\<string\> | arXiv categories |
| `sent_at` | timestamp | Server timestamp at write |
| `score` | int | `upvotes − downvotes`, updated by the webhook |
| `last_vote_at` | timestamp \| null | Null until the first reaction lands |

**`profiles`** — one doc per profile generation. Doc ID is the timestamp (`YYYYMMDDTHHMMSSZ`) so they sort lexicographically.

| Field | Type | Notes |
|---|---|---|
| `generated_at` | timestamp | Server timestamp at write |
| `liked_themes` | list\<string\> | 5–10 concrete research themes |
| `disliked_themes` | list\<string\> | Themes the user passes on |
| `prose_summary` | string | 3–5 sentence narrative for ranking prompts |
| `source_paper_ids` | list\<string\> | arXiv IDs the profile was built from |
| `source_paper_count` | int | Length of `source_paper_ids` |

## Setup

### Prerequisites

* A GCP project with billing enabled
* [Anthropic API key](https://console.anthropic.com) — costs ~£5–7/month at the default config
* A Telegram bot (create via [@BotFather](https://t.me/BotFather))
* [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
* [gcloud CLI](https://cloud.google.com/sdk/docs/install) authenticated to your project
* A GCS bucket for Terraform state (referenced in `main.tf`'s `backend "gcs"` block)

### 1. Create the Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) and send `/newbot`. Save the bot token.
2. Create a channel and add the bot as an admin with **Post Messages** permission.
3. Get the chat ID — either use `@your_channel_name` or send a test message and visit `https://api.telegram.org/bot<TOKEN>/getUpdates`.

### 2. Deploy Infrastructure with Terraform

The Terraform config creates Secret Manager *shells* but not their values — populate those after `apply`.

```bash
cd infrastructure

cat > terraform.tfvars <<EOF
project_name         = "your-gcp-project-id"
project_number       = "123456789012"
region               = "europe-west2"
arxiv_categories     = "cs.AI,cs.LG,cs.CL,cs.SE,cs.IR"
arxiv_max_results    = 300
top_n_papers         = 5
claude_model_ranking = "claude-sonnet-4-5"
claude_model_summary = "claude-haiku-4-5-20251001"
claude_model_profile = "claude-sonnet-4-5"
papers_collection    = "sent_papers"
profiles_collection  = "profiles"
unvoted_ttl_days     = 14
vote_threshold       = 10
EOF

terraform init -backend-config="bucket=your-tfstate-bucket"
terraform plan
terraform apply
```

This provisions every GCP resource: APIs, the Firestore `(default)` database, four service accounts, IAM bindings, four Secret Manager secrets, the digest Cloud Run Job, the profile-regen Cloud Run Job, the webhook Cloud Run Service, the Cloud Tasks queue, and the Cloud Scheduler trigger.

### 3. Populate Secrets

The first three are values you already have. The fourth — the Telegram webhook secret — is generated here. Telegram will echo it back in the `X-Telegram-Bot-Api-Secret-Token` header on every callback, and the Go webhook constant-time-compares it before doing any work, so a leak is a full bypass. Generate it directly into Secret Manager and never let it touch disk or shell history:

```bash
echo -n "sk-ant-..."     | gcloud secrets versions add anthropic-api-key --data-file=-
echo -n "7123456:ABC..." | gcloud secrets versions add bot_key           --data-file=-
echo -n "@your_channel"  | gcloud secrets versions add telegram-chat-id  --data-file=-

# Generate a 64-char hex secret straight into Secret Manager.
openssl rand -hex 32 | tr -d '\n' \
  | gcloud secrets versions add telegram-webhook-secret --data-file=-
```

The webhook secret accepts `A-Z`, `a-z`, `0-9`, `_`, `-`, length 1–256. 32 random bytes hex-encoded is well within that and gives 256 bits of entropy.

### 4. Build and Deploy the Containers

Each service has its own `cloudbuild.yaml`. Run from each directory:

```bash
( cd digest_job             && gcloud builds submit --project=YOUR_PROJECT_ID )
( cd webhook_service        && gcloud builds submit --project=YOUR_PROJECT_ID )
( cd profile_generation_job && gcloud builds submit --project=YOUR_PROJECT_ID )
```

Each build pushes to Container Registry and updates the corresponding Cloud Run resource.

### 5. Register the Telegram Webhook

Get the webhook service URL from Terraform output (or `gcloud run services describe webhook-service --region=$REGION --format='value(status.url)'`), then read the secret back from Secret Manager and pass it to Telegram's `setWebhook`:

```bash
WEBHOOK_URL=$(gcloud run services describe webhook-service \
                --region=europe-west2 \
                --format='value(status.url)')
WEBHOOK_SECRET=$(gcloud secrets versions access latest \
                   --secret=telegram-webhook-secret)

curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
     -d "url=${WEBHOOK_URL}/" \
     -d "secret_token=${WEBHOOK_SECRET}" \
     -d 'allowed_updates=["message_reaction_count"]'
```

`allowed_updates` is important — without it Telegram will not send reaction events. After this call only Secret Manager and Telegram hold the value; nothing on your local machine does.

To verify Telegram has accepted the webhook:

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/getWebhookInfo"
```

The response should show your Cloud Run URL and `has_custom_certificate: false`. Telegram does *not* expose `secret_token` in this response (it's write-only), so trust the absence of `last_error_message` instead.

### 6. Test It

```bash
# Trigger the digest manually
gcloud run jobs execute arxiv-digest --region=europe-west2

# Trigger profile regen manually
gcloud run jobs execute profile-job --region=europe-west2

# Check webhook logs
gcloud run services logs read webhook-service --region=europe-west2 --limit=50
```

## Runtime Configuration

### `digest_job`

| Variable | Code default | Description |
|---|---|---|
| `ARXIV_CATEGORIES` | `cs.AI,cs.LG,cs.CL,cs.SE,cs.IR` | arXiv categories to fetch |
| `ARXIV_MAX_RESULTS` | `100` | Papers fetched from arXiv |
| `TOP_N_PAPERS` | `5` | Papers in the digest |
| `CLAUDE_MODEL_RANKING` | (Terraform) | Model for ranking |
| `CLAUDE_MODEL_SUMMARY` | (Terraform) | Model for PDF summarisation |
| `PAPERS_COLLECTION` | `sent_papers` | Firestore collection for sent papers |
| `PROFILES_COLLECTION` | `profiles` | Firestore collection for profiles |
| `USER_INTERESTS` | See `main.py` | Fallback profile if Firestore has none yet |
| `ANTHROPIC_API_KEY` | — | From Secret Manager |
| `TELEGRAM_BOT_TOKEN` | — | From Secret Manager |
| `TELEGRAM_CHAT_ID` | — | From Secret Manager |

### `webhook_service`

| Variable | Description |
|---|---|
| `GCP_PROJECT_ID` | Project ID for Firestore + Cloud Tasks |
| `LOCATION` | Region of the Cloud Tasks queue |
| `QUEUE_ID` | Fully-qualified Cloud Tasks queue name |
| `PAPERS_COLLECTION_NAME` | Firestore collection for sent papers |
| `PROFILE_COLLECTION_NAME` | Firestore collection for profiles |
| `GENERATION_URL` | Cloud Run admin API URL for the profile job |
| `VOTE_THRESHOLD` | Updated-paper count that triggers regen |
| `WEBHOOK_SECRET` | From Secret Manager — validated against `X-Telegram-Bot-Api-Secret-Token` |

### `profile_generation_job`

| Variable | Default | Description |
|---|---|---|
| `PAPERS_COLLECTION` | `sent_papers` | |
| `PROFILES_COLLECTION` | `profiles` | |
| `UNVOTED_TTL_DAYS` | `14` | Papers with no reactions are pruned after this |
| `CLAUDE_MODEL_PROFILE` | (Terraform) | Model for profile generation |
| `ANTHROPIC_API_KEY` | — | From Secret Manager |

## Terraform Variables

| Variable | Example | Description |
|---|---|---|
| `project_name` | `my-gcp-project` | GCP project ID |
| `project_number` | `123456789012` | GCP project number |
| `region` | `europe-west2` | Region for all resources |
| `arxiv_categories` | `cs.AI,cs.LG,cs.CL,cs.SE,cs.IR` | arXiv categories to track |
| `arxiv_max_results` | `300` | Papers fetched per run |
| `top_n_papers` | `5` | Papers included in the digest |
| `claude_model_ranking` | `claude-sonnet-4-5` | Model for ranking |
| `claude_model_summary` | `claude-haiku-4-5-20251001` | Model for summarisation |
| `claude_model_profile` | `claude-sonnet-4-5` | Model for profile generation |
| `papers_collection` | `sent_papers` | Firestore collection |
| `profiles_collection` | `profiles` | Firestore collection |
| `unvoted_ttl_days` | `14` | TTL for unvoted papers |
| `vote_threshold` | `10` | Updated-paper count to trigger regen |

## Design Decisions

**Two-model approach.** Sonnet for ranking (stronger reasoning over many abstracts), Haiku for per-paper summarisation (simpler task, runs N× per day, materially cheaper). Decoupled via env vars in Terraform.

**Structured output everywhere.** Ranking, summarisation, and profile generation all use Anthropic's JSON-schema output, validated by Pydantic (`PaperSelection`, `PaperSummary`, `Profile`). No regex, no retry-on-bad-JSON.

**Firestore over GCS for state.** Atomicity (`update` on a doc), free tier covers this scale, queries beat ad-hoc JSON parsing. Doc IDs use the Telegram `message_id` so the webhook can look up the target paper without a secondary index.

**Cloud Tasks over Pub/Sub for profile regen.** Single producer, single consumer, exactly the workload Cloud Tasks is designed for: queue concurrency=1, exponential backoff, OIDC auth to Cloud Run baked in. Pub/Sub would have been more machinery for the same outcome.

**Cloud Run Job for daily digest, Cloud Run Service for the webhook.** Run-to-completion vs. always-listening. Different primitives, applied where each fits.

**Incremental profile updates.** The profile generator fetches only papers voted on *since* the last profile, and passes the prior profile to Claude as context. Avoids both reprocessing every paper ever and discarding hard-won prior signal.

**Go for the webhook.** Boring choice for a small HTTP server: low cold-start, single static binary in a distroless image, fewer moving parts. Also a deliberate choice to broaden language exposure in the project.

**Dedicated service accounts per service.** Five SAs in total — digest runner, scheduler invoker, webhook, profile-job runner, Cloud Tasks invoker — each with the minimum IAM it needs. No use of the default compute SA.

**Webhook auth via Telegram secret token.** Telegram's `setWebhook` accepts a `secret_token` parameter that's echoed back in `X-Telegram-Bot-Api-Secret-Token` on every callback. The Go handler rejects non-POST methods, constant-time-compares the header against the configured secret (avoiding timing leaks on the secret length), and only then parses the body — which itself is capped via `http.MaxBytesReader` so a malicious caller can't OOM the service with an oversized payload. The token lives in Secret Manager and is mounted as an env var, never in source or Terraform state.

**Terraform-from-the-start.** Everything reproducible from `terraform apply` + four secret pushes. Secret values are intentionally not in state.

**Rate limiting on PDF processing.** A sleep between summarisation calls keeps within arXiv rate limits and Anthropic per-minute token limits on larger PDFs.

## Costs

| Component | Cost |
|---|---|
| Cloud Run (Jobs + Service) | Free tier |
| Cloud Scheduler | Free (3 jobs/month) |
| Cloud Tasks | Free tier (1M ops/month) |
| Firestore | Free tier (1 GiB, 50k reads/day) |
| Claude API (ranking, Sonnet) | ~£0.02/day |
| Claude API (summaries, Haiku) | ~£0.20/day |
| Claude API (profile regen) | Negligible (runs ~weekly) |
| Secret Manager | Negligible |
| arXiv API | Free |
| Telegram API | Free |
| **Total** | **~£5–7/month** |

## License

MIT