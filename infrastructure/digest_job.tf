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