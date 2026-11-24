output "cloud_run_service_url" {
  description = "HTTPS endpoint of the deployed Sentinel SRE Cloud Run service."
  value       = google_cloud_run_v2_service.sentinel_api.uri
}

output "runtime_service_account_email" {
  description = "Least-privilege runtime service account email."
  value       = google_service_account.agent_runtime.email
}

output "vertex_agent_engine_resource_name" {
  description = "Provisioned Vertex AI Agent Engine resource name."
  value       = google_vertex_ai_reasoning_engine.sentinel_engine.name
}

output "runbook_datastore_name" {
  description = "Vertex AI Search Data Store resource name for SRE runbooks."
  value       = google_discovery_engine_data_store.sre_runbooks.name
}

output "dlp_deidentify_template_name" {
  description = "Cloud DLP de-identify template resource name for PII redaction."
  value       = google_data_loss_prevention_deidentify_template.pii_deidentify.name
}

output "model_armor_template_name" {
  description = "Model Armor guardrail template resource name."
  value       = google_model_armor_template.sentinel_guardrail.name
}

output "bigquery_analytics_dataset" {
  description = "BigQuery dataset receiving structured Intent-vs-Outcome audit logs."
  value       = google_bigquery_dataset.agent_analytics.dataset_id
}

output "artifact_registry_url" {
  description = "Artifact Registry repository path for container pushes."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.agent_images.repository_id}"
}
