resource "google_cloud_run_v2_service" "sentinel_api" {
  name     = "${var.service_name}-${var.environment}"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  template {
    service_account = google_service_account.agent_runtime.email

    scaling {
      min_instance_count = var.min_instance_count
      max_instance_count = var.max_instance_count
    }

    containers {
      image = var.container_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        cpu_idle = true
      }

      env {
        name  = "SENTINEL_ENV"
        value = var.environment
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "GOOGLE_CLOUD_LOCATION"
        value = var.region
      }
      env {
        name  = "GOOGLE_GENAI_USE_VERTEXAI"
        value = "true"
      }
      env {
        name  = "SENTINEL_FAST_MODEL"
        value = var.fast_model
      }
      env {
        name  = "SENTINEL_REASONING_MODEL"
        value = var.reasoning_model
      }
      env {
        name  = "VERTEX_AGENT_ENGINE_ID"
        value = google_vertex_ai_reasoning_engine.sentinel_engine.name
      }
      env {
        name  = "VERTEX_AI_SEARCH_DATASTORE_ID"
        value = google_discovery_engine_data_store.sre_runbooks.name
      }
      env {
        name  = "SESSION_BACKEND"
        value = "vertex_ai"
      }
      env {
        name  = "MEMORY_BACKEND"
        value = "vertex_ai_memory_bank"
      }
      env {
        name  = "ENABLE_CLOUD_TRACE"
        value = "true"
      }
      env {
        name  = "OTEL_SERVICE_NAME"
        value = var.service_name
      }
      env {
        name  = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"
        value = "NO_CONTENT"
      }
      env {
        name  = "ENABLE_CLOUD_DLP"
        value = "true"
      }
      env {
        name  = "DLP_INSPECT_TEMPLATE"
        value = google_data_loss_prevention_inspect_template.pii_inspect.name
      }
      env {
        name  = "DLP_DEIDENTIFY_TEMPLATE"
        value = google_data_loss_prevention_deidentify_template.pii_deidentify.name
      }
      env {
        name  = "ENABLE_MODEL_ARMOR"
        value = "true"
      }
      env {
        name  = "MODEL_ARMOR_PROMPT_TEMPLATE"
        value = google_model_armor_template.sentinel_guardrail.name
      }
      env {
        name  = "MODEL_ARMOR_RESPONSE_TEMPLATE"
        value = google_model_armor_template.sentinel_guardrail.name
      }
      env {
        name  = "FAIL_CLOSED_SECRETS"
        value = "true"
      }
      env {
        name  = "BQ_ANALYTICS_DATASET_ID"
        value = google_bigquery_dataset.agent_analytics.dataset_id
      }

      # Secure Secret Manager environment injection — ZERO hardcoded keys!
      env {
        name = "SENTINEL_PAGERDUTY_API_KEY"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.agent_secrets["SENTINEL_PAGERDUTY_API_KEY"].secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "SENTINEL_DEPLOY_API_TOKEN"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.agent_secrets["SENTINEL_DEPLOY_API_TOKEN"].secret_id
            version = "latest"
          }
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        initial_delay_seconds = 10
        period_seconds        = 10
        failure_threshold     = 5
      }

      liveness_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds = 30
      }
    }
  }

  depends_on = [
    google_project_service.enabled_apis,
    google_secret_manager_secret_iam_member.runtime_secret_accessor,
    google_vertex_ai_reasoning_engine.sentinel_engine,
  ]
}
