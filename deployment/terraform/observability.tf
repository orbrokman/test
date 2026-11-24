resource "google_bigquery_dataset" "agent_analytics" {
  dataset_id                  = var.bq_dataset_id
  friendly_name               = "Sentinel SRE AgentOps Telemetry & Intent-Outcome Audit"
  description                 = "Stores structured JSON intent/outcome logs and evaluation metrics."
  location                    = var.region
  default_table_expiration_ms = 7776000000 # 90 days

  labels = {
    app         = var.service_name
    environment = var.environment
  }

  depends_on = [google_project_service.enabled_apis]
}

# Route structured Intent-vs-Outcome JSON audit logs directly to BigQuery
resource "google_logging_project_sink" "intent_outcome_audit_sink" {
  name        = "${var.service_name}-intent-outcome-sink"
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.agent_analytics.dataset_id}"
  filter      = "jsonPayload.service=\"${var.service_name}\" AND (jsonPayload.phase=\"intent\" OR jsonPayload.phase=\"outcome\" OR jsonPayload.event=\"guardrail_input_blocked\")"

  unique_writer_identity = true

  bigquery_options {
    use_partitioned_tables = true
  }
}

resource "google_bigquery_dataset_iam_member" "sink_bq_writer" {
  dataset_id = google_bigquery_dataset.agent_analytics.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.intent_outcome_audit_sink.writer_identity
}

# Log-based metric tracking security guardrail interventions
resource "google_logging_metric" "guardrail_blocks_metric" {
  name        = "${replace(var.service_name, "-", "_")}_guardrail_blocks"
  description = "Count of prompts or tool calls blocked by Sentinel SecurityGuardrailsPlugin."
  filter      = "jsonPayload.service=\"${var.service_name}\" AND (jsonPayload.event=\"guardrail_input_blocked\" OR jsonPayload.event=\"guardrail_tool_blocked\" OR jsonPayload.event=\"guardrail_model_armor_blocked\")"

  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
  }

  depends_on = [google_project_service.enabled_apis]
}

# Cloud Monitoring Alert Policy on sustained guardrail / security attack spikes
resource "google_monitoring_alert_policy" "guardrail_spike_alert" {
  display_name = "Sentinel SRE Agent — Security Guardrail Block Spike"
  combiner     = "OR"

  conditions {
    display_name = "More than 5 guardrail blocks in 5 minutes"
    condition_threshold {
      filter          = "metric.type=\"logging.googleapis.com/user/${google_logging_metric.guardrail_blocks_metric.name}\" AND resource.type=\"cloud_run_revision\""
      duration        = "300s"
      comparison      = "COMPARISON_GT"
      threshold_value = 5

      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_SUM"
      }
    }
  }

  depends_on = [google_logging_metric.guardrail_blocks_metric]
}
