resource "google_cloud_run_v2_service" "webhook_service" {
  name     = "webhook-service"
  location = var.region

  ingress = "INGRESS_TRAFFIC_ALL"

  template {
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
        value = var.papers_collection
      }

      env {
        name = "GENERATION_URL"
        value = "${google_cloud_run_v2_job.profile_job.id}:latest"
      }

      env {
        name = "QUEUE_ID"
        value = google_cloud_tasks_queue.profile_regen.id
      }
    }
  }

  lifecycle {
    ignore_changes = [ template[0].containers[0].image ]
  }
}

resource "google_cloud_run_v2_service_iam_member" "webhook_public" {
  project  = google_cloud_run_v2_service.webhook_service.project
  location = google_cloud_run_v2_service.webhook_service.location
  name     = google_cloud_run_v2_service.webhook_service.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}