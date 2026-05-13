resource "google_service_account" "webhook" {
  account_id = "arxiv-webhook"
}

resource "google_cloud_run_v2_service" "webhook_service" {
  name     = "webhook-service"
  location = var.region
  ingress = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true

  template {
    service_account = google_service_account.webhook.email
    scaling {
      min_instance_count = 0
    }
    containers {
      image = "us-docker.pkg.dev/cloudrun/container/hello"
      env {
        name = "GCP_PROJECT_ID"
        value = var.project_name
      }

      env {
        name = "DATABASE_URL"
        value = "(default)"
      }

      env {
        name = "PAPERS_COLLECTION_NAME"
        value = var.papers_collection
      }

      env {
        name = "PROFILE_COLLECTION_NAME"
        value = var.profiles_collection
      }

      env {
        name = "GENERATION_URL"
        value = "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${var.project_name}/jobs/${google_cloud_run_v2_job.profile_job.name}:run"
      }

      env {
        name = "QUEUE_ID"
        value = google_cloud_tasks_queue.profile_regen.id
      }

      env {
        name = "LOCATION"
        value = google_cloud_tasks_queue.profile_regen.location
      }

      env {
        name = "VOTE_THRESHOLD"
        value = var.vote_threshold
      }

      env {
        name = "WEBHOOK_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.telegram_webhook_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  lifecycle {
    ignore_changes = [ template[0].containers[0].image ]
  }
}

resource "google_project_iam_member" "firestore_webhook" {
  member = google_service_account.webhook.member
  role = "roles/datastore.editor"
  project = var.project_name
}

resource "google_service_account_iam_binding" "webhook_to_tasks" {
  service_account_id = google_service_account.tasks_invoker.id
  members = [google_service_account.webhook.member]
  role = "roles/iam.serviceAccountUser"
}