# Service Account

resource "google_service_account" "profile_job" {
  account_id   = "profile-job-runner"
  display_name = "Profile Generation Cloud Run Job"
}

resource "google_secret_manager_secret_iam_member" "profile_anthropic_access" {
  secret_id = google_secret_manager_secret.anthropic_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.profile_job.email}"
}

resource "google_project_iam_member" "profile_firestore_access" {
  project = var.project_name
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.profile_job.email}"
}

# Actual Job

resource "google_cloud_run_v2_job" "profile_job" {
  name     = "profile-job"
  location = var.region

  template {
    task_count = 1

    template {
      service_account = google_service_account.profile_job.email
      max_retries     = 0
      timeout         = "600s"

      containers {
        image = var.profile_image

        resources {
          limits = {
            memory = "512Mi"
            cpu    = "1"
          }
        }

        env {
          name  = "FIRESTORE_COLLECTION"
          value = var.papers_collection
        }

        env {
          name  = "PROFILES_COLLECTION"
          value = var.profiles_collection
        }

        env {
          name  = "UNVOTED_TTL_DAYS"
          value = tostring(var.unvoted_ttl_days)
        }

        env {
          name  = "CLAUDE_MODEL_PROFILE"
          value = var.claude_model_profile
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
      }
    }
  }

  depends_on = [
    google_project_service.apis["run.googleapis.com"],
    google_firestore_database.default,
  ]
}