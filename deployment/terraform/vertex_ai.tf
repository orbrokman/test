# Vertex AI Agent Engine (Managed ADK Runtime + Memory Bank + Sessions)
resource "google_vertex_ai_reasoning_engine" "sentinel_engine" {
  provider     = google-beta
  project      = var.project_id
  region       = var.region
  display_name = "${var.service_name}-${var.environment}"
  description  = "Sentinel Enterprise SRE Multi-Agent ADK Runtime with Memory Bank."

  depends_on = [
    google_project_service.enabled_apis,
    google_project_iam_member.agent_runtime_roles,
  ]
}

# Google Cloud DLP (Sensitive Data Protection) Inspect & De-identify Templates
resource "google_data_loss_prevention_inspect_template" "pii_inspect" {
  parent       = "projects/${var.project_id}/locations/global"
  display_name = "Sentinel SRE PII Inspect Template"
  description  = "Detects PII and cloud credentials prior to logging and memory persistence."

  inspect_config {
    min_likelihood = "LIKELY"
    info_types {
      name = "EMAIL_ADDRESS"
    }
    info_types {
      name = "PHONE_NUMBER"
    }
    info_types {
      name = "US_SOCIAL_SECURITY_NUMBER"
    }
    info_types {
      name = "CREDIT_CARD_NUMBER"
    }
    info_types {
      name = "IP_ADDRESS"
    }
    info_types {
      name = "GCP_API_KEY"
    }
    info_types {
      name = "AUTH_TOKEN"
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_data_loss_prevention_deidentify_template" "pii_deidentify" {
  parent       = "projects/${var.project_id}/locations/global"
  display_name = "Sentinel SRE PII De-identify Template"
  description  = "Replaces sensitive entities with info-type placeholders before storage."

  deidentify_config {
    info_type_transformations {
      transformations {
        primitive_transformation {
          replace_with_info_type_config {}
        }
      }
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

# Google Cloud Model Armor Guardrail Template for Prompt & Response Screening
resource "google_model_armor_template" "sentinel_guardrail" {
  provider    = google-beta
  project     = var.project_id
  location    = var.region
  template_id = "${var.service_name}-guardrail"

  filter_config {
    pi_and_jailbreak_filter_settings {
      filter_enforcement = "ENABLED"
      confidence_level   = "MEDIUM_AND_ABOVE"
    }
    malicious_uri_filter_settings {
      filter_enforcement = "ENABLED"
    }
    rai_settings {
      rai_filters {
        filter_type      = "DANGEROUS"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HATE_SPEECH"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
      rai_filters {
        filter_type      = "HARASSMENT"
        confidence_level = "MEDIUM_AND_ABOVE"
      }
    }
  }

  depends_on = [google_project_service.enabled_apis]
}
