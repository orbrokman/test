resource "google_artifact_registry_repository" "agent_images" {
  location      = var.region
  repository_id = var.service_name
  description   = "Container images for Sentinel SRE ADK Agent."
  format        = "DOCKER"

  cleanup_policies {
    id     = "keep-recent-releases"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_storage_bucket" "agent_artifacts" {
  name                        = "${var.project_id}-${var.service_name}-artifacts"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = var.environment != "production"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

# Managed Vertex AI Search Data Store for Enterprise SRE Runbooks & Postmortems
resource "google_discovery_engine_data_store" "sre_runbooks" {
  location                    = "global"
  data_store_id               = "${var.service_name}-runbooks"
  display_name                = "Sentinel SRE Incident Runbooks & Postmortems"
  industry_vertical           = "GENERIC"
  content_config              = "CONTENT_REQUIRED"
  solution_types              = ["SOLUTION_TYPE_SEARCH"]
  create_advanced_site_search = false

  depends_on = [google_project_service.enabled_apis]
}

# Persistent Cloud SQL PostgreSQL backing ADK DatabaseSessionService
resource "google_sql_database_instance" "sessions_postgres" {
  count = var.enable_cloud_sql ? 1 : 0

  name                = "${var.service_name}-sessions-${var.environment}"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.environment == "production"

  settings {
    tier              = "db-f1-micro"
    availability_type = var.environment == "production" ? "REGIONAL" : "ZONAL"

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = var.environment == "production"
    }

    ip_configuration {
      ipv4_enabled = true
      ssl_mode     = "ENCRYPTED_ONLY"
    }
  }

  depends_on = [google_project_service.enabled_apis]
}

resource "google_sql_database" "adk_sessions" {
  count = var.enable_cloud_sql ? 1 : 0

  name     = "sentinel_sessions"
  instance = google_sql_database_instance.sessions_postgres[0].name
}
