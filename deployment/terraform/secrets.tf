# Secret Manager containers ONLY — never store plaintext secret versions in Terraform state!
# Populate secret versions out-of-band via:
#   echo -n "$VALUE" | gcloud secrets versions add <SECRET_ID> --data-file=-

locals {
  managed_secrets = toset([
    "SENTINEL_PAGERDUTY_API_KEY",
    "SENTINEL_DEPLOY_API_TOKEN",
    "SENTINEL_DB_PASSWORD",
  ])
}

resource "google_secret_manager_secret" "agent_secrets" {
  for_each = local.managed_secrets

  project   = var.project_id
  secret_id = each.value

  labels = {
    app         = var.service_name
    environment = var.environment
    managed_by  = "terraform"
  }

  replication {
    auto {}
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_secret_manager_secret_iam_member" "runtime_secret_accessor" {
  for_each = local.managed_secrets

  project   = var.project_id
  secret_id = google_secret_manager_secret.agent_secrets[each.value].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.agent_runtime.email}"
}
