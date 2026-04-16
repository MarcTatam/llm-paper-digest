terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
  }
  backend "gcs" {
    prefix = "terraform"
  }
}

# ─────────────────────────────────────────────
# APIs
# ─────────────────────────────────────────────

resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudscheduler.googleapis.com",
    "secretmanager.googleapis.com",
    "containerregistry.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}

# ─────────────────────────────────────────────
# Secrets (data sources — these already exist)
# ─────────────────────────────────────────────

resource "google_secret_manager_secret" "anthropic_api_key" {
  secret_id = "anthropic-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "telegram_bot_token" {
  secret_id = "bot_key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret" "telegram_chat_id" {
  secret_id = "telegram-chat-id"
  replication {
    auto {}
  }
}

resource "google_service_account" "arxiv_digest" {
  account_id   = "arxiv-digest-runner"
  display_name = "arXiv Digest Cloud Run Job"
}

# Allow the SA to read secrets
resource "google_secret_manager_secret_iam_member" "anthropic_key_access" {
  secret_id = google_secret_manager_secret.anthropic_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.arxiv_digest.email}"
}

resource "google_secret_manager_secret_iam_member" "bot_token_access" {
  secret_id = google_secret_manager_secret.telegram_bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.arxiv_digest.email}"
}

resource "google_secret_manager_secret_iam_member" "chat_id_access" {
  secret_id = google_secret_manager_secret.telegram_chat_id.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.arxiv_digest.email}"
}

# ─────────────────────────────────────────────
# Cloud Run Job
# ─────────────────────────────────────────────

resource "google_cloud_run_v2_job" "arxiv_digest" {
  name     = "arxiv-digest"
  location = var.region

  template {
    task_count = 1

    template {
      service_account = google_service_account.arxiv_digest.email
      max_retries     = 0
      timeout         = "600s"

      containers {
        image = var.image

        resources {
          limits = {
            memory = "512Mi"
            cpu    = "1"
          }
        }

        env {
          name  = "ARXIV_CATEGORIES"
          value = var.arxiv_categories
        }

        env {
          name  = "ARXIV_MAX_RESULTS"
          value = tostring(var.arxiv_max_results)
        }

        env {
          name  = "TOP_N_PAPERS"
          value = tostring(var.top_n_papers)
        }

        env {
          name  = "CLAUDE_MODEL_RANKING"
          value = var.claude_model_ranking
        }

        env {
          name  = "CLAUDE_MODEL_SUMMARY"
          value = var.claude_model_summary
        }

        env {
          name = "ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.anthropic_api_key.secret_id
              version = "latest"
            }
          }
        }

        env {
          name = "TELEGRAM_BOT_TOKEN"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.telegram_bot_token.secret_id
              version = "latest"
            }
          }
        }

        env {
          name = "TELEGRAM_CHAT_ID"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.telegram_chat_id.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [google_project_service.apis["run.googleapis.com"]]
}

# ─────────────────────────────────────────────
# Cloud Scheduler
# ─────────────────────────────────────────────

resource "google_service_account" "scheduler_invoker" {
  account_id   = "arxiv-digest-scheduler"
  display_name = "arXiv Digest Scheduler Invoker"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_can_invoke" {
  name     = google_cloud_run_v2_job.arxiv_digest.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler_invoker.email}"
}

resource "google_cloud_scheduler_job" "arxiv_daily_digest" {
  name      = "arxiv-daily-digest"
  region    = var.region
  schedule  = "0 8 * * 1-5"
  time_zone = "Europe/London"

  http_target {
    http_method = "POST"
    uri         = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_name}/jobs/${google_cloud_run_v2_job.arxiv_digest.name}:run"

    oauth_token {
      service_account_email = google_service_account.scheduler_invoker.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_project_service.apis["cloudscheduler.googleapis.com"]]
}
