resource "google_service_account" "tasks_invoker" {
  account_id   = "profile-tasks-invoker"
  display_name = "Cloud Tasks -> Profile Job Invoker"
}

resource "google_cloud_run_v2_job_iam_member" "tasks_can_invoke" {
  name     = google_cloud_run_v2_job.profile_job.name
  location = var.region
  role     = "roles/run.invoker"
  member   = google_service_account.tasks_invoker.member
}

resource "google_cloud_tasks_queue" "profile_regen" {
  name     = "profile-regen-queue"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 1
    max_dispatches_per_second = 1
  }

  retry_config {
    max_attempts       = 3
    min_backoff        = "30s"
    max_backoff        = "300s"
    max_doublings      = 2
    max_retry_duration = "3600s"
  }

  http_target {
    http_method = "POST"

    oauth_token {
      service_account_email = google_service_account.tasks_invoker.email
    }
  }

  depends_on = [google_project_service.apis["cloudtasks.googleapis.com"]]
}

resource "google_cloud_tasks_queue_iam_binding" "allow_queueing" {
  members = [google_service_account.webhook.member]
  role = "roles/cloudtasks.enqueuer"
  name = google_cloud_tasks_queue.profile_regen.name
}