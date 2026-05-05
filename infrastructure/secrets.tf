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

# Allow the SA to read secrets
resource "google_secret_manager_secret_iam_member" "anthropic_key_access" {
  secret_id = google_secret_manager_secret.anthropic_api_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.arxiv_digest.member
}

resource "google_secret_manager_secret_iam_member" "bot_token_access" {
  secret_id = google_secret_manager_secret.telegram_bot_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.arxiv_digest.member
}

resource "google_secret_manager_secret_iam_member" "chat_id_access" {
  secret_id = google_secret_manager_secret.telegram_chat_id.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = google_service_account.arxiv_digest.member
}