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