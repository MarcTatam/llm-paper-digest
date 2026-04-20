resource "google_service_account" "tasks_invoker" {
  account_id   = "profile-tasks-invoker"
  display_name = "Cloud Tasks -> Profile Job Invoker"
}

resource "google_cloud_run_v2_job_iam_member" "tasks_can_invoke" {
  name     = google_cloud_run_v2_job.profile_job.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tasks_invoker.email}"
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

  depends_on = [google_project_service.apis["cloudtasks.googleapis.com"]]
}