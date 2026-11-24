locals {
  runtime_sa_roles = toset([
    "roles/aiplatform.user",
    "roles/bigquery.dataEditor",
    "roles/cloudsql.client",
    "roles/cloudtrace.agent",
    "roles/discoveryengine.viewer",
    "roles/dlp.user",
    "roles/logging.logWriter",
    "roles/modelarmor.user",
    "roles/monitoring.metricWriter",
    "roles/storage.objectUser",
  ])
}

resource "google_service_account" "agent_runtime" {
  account_id   = "${var.service_name}-sa"
  display_name = "Sentinel SRE Agent Runtime Service Account (${var.environment})"
  description  = "Least-privilege runtime identity for Sentinel ADK multi-agent system."
  depends_on   = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "agent_runtime_roles" {
  for_each = local.runtime_sa_roles

  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_runtime.email}"
}

resource "google_service_account" "cicd_deployer" {
  account_id   = "${var.service_name}-cicd"
  display_name = "Sentinel SRE Agent CI/CD Deployer"
  description  = "Identity used by GitHub Actions / Cloud Build for automated evals and deploys."
  depends_on   = [google_project_service.enabled_apis]
}

resource "google_project_iam_member" "cicd_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = "serviceAccount:${google_service_account.cicd_deployer.email}"
}

resource "google_service_account_iam_member" "cicd_act_as_runtime" {
  service_account_id = google_service_account.agent_runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cicd_deployer.email}"
}
