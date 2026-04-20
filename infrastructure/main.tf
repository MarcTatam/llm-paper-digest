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
    "firestore.googleapis.com",
    "cloudtasks.googleapis.com",
  ])

  service            = each.value
  disable_on_destroy = false
}
