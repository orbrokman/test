variable "project_id" {
  description = "Target Google Cloud project ID for Sentinel SRE Agent."
  type        = string
}

variable "region" {
  description = "Primary Google Cloud region (e.g., us-central1)."
  type        = string
  default     = "us-central1"
}

variable "environment" {
  description = "Deployment environment label (staging or production)."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be either 'staging' or 'production'."
  }
}

variable "service_name" {
  description = "Base name for Cloud Run, service accounts, and Vertex resources."
  type        = string
  default     = "sentinel-sre-agent"
}

variable "container_image" {
  description = "Artifact Registry image URI built for Cloud Run deployment."
  type        = string
  default     = "us-central1-docker.pkg.dev/PROJECT_ID/sentinel-sre-agent/sentinel-sre-agent:latest"
}

variable "fast_model" {
  description = "Low-latency Gemini model used for triage, diagnostics, and compaction."
  type        = string
  default     = "gemini-2.5-flash"
}

variable "reasoning_model" {
  description = "High-capability Gemini model used for root-cause synthesis and critic self-eval."
  type        = string
  default     = "gemini-2.5-pro"
}

variable "bq_dataset_id" {
  description = "BigQuery dataset ID for structured AgentOps analytics and audit sinks."
  type        = string
  default     = "sentinel_sre_analytics"
}

variable "enable_cloud_sql" {
  description = "Provision a managed Cloud SQL PostgreSQL instance for ADK DatabaseSessionService."
  type        = bool
  default     = true
}

variable "min_instance_count" {
  description = "Minimum warm Cloud Run instances (0 for scale-to-zero in staging)."
  type        = number
  default     = 0
}

variable "max_instance_count" {
  description = "Maximum Cloud Run auto-scaling instances."
  type        = number
  default     = 10
}
