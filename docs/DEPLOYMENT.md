# Deployment & Infrastructure as Code Guide

Sentinel SRE Agent supports Google Cloud deployments via **Terraform IaC**, **Google ADK CLI (`adk`)**, **Google Agent CLI (`agents-cli`)**, and **Agent Starter Pack** patterns.

---

## 1. Prerequisites

- Google Cloud SDK (`gcloud`) authenticated with access to a target GCP project
- Terraform >= 1.6.0
- Python >= 3.11 (`uv` recommended)
- Optional: `google-agents-cli` (`uv tool install google-agents-cli`) or `agent-starter-pack`

```bash
gcloud auth login
gcloud auth application-default login
export GOOGLE_CLOUD_PROJECT="your-gcp-project-id"
export GOOGLE_CLOUD_LOCATION="us-central1"
gcloud config set project "$GOOGLE_CLOUD_PROJECT"
```

---

## 2. Provision Infrastructure with Terraform

All GCP dependencies (APIs, least-privilege IAM service accounts, Secret Manager containers, Artifact Registry, GCS artifacts bucket, Vertex AI Search datastore, Cloud SQL PostgreSQL, Vertex AI Agent Engine, Cloud DLP templates, Model Armor template, Cloud Run v2 service, BigQuery Agent Analytics dataset, Cloud Logging audit sink, and Cloud Monitoring alerts) are declared in [`deployment/terraform/`](../deployment/terraform/).

```bash
cd deployment/terraform
cp terraform.tfvars.example terraform.tfvars
# Update `project_id` and `container_image` in terraform.tfvars

terraform init
terraform fmt -check -recursive
terraform validate
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

---

## 3. Populate Secret Manager Versions (Zero Hardcoded Keys)

Terraform creates only the Secret Manager secret containers and grants `roles/secretmanager.secretAccessor` to the runtime service account (`sentinel-sre-agent-sa`). Populate secret versions out-of-band:

```bash
echo -n "$PAGERDUTY_API_KEY" | gcloud secrets versions add SENTINEL_PAGERDUTY_API_KEY \
  --project="$GOOGLE_CLOUD_PROJECT" --data-file=-

echo -n "$DEPLOY_API_TOKEN" | gcloud secrets versions add SENTINEL_DEPLOY_API_TOKEN \
  --project="$GOOGLE_CLOUD_PROJECT" --data-file=-

echo -n "$DB_PASSWORD" | gcloud secrets versions add SENTINEL_DB_PASSWORD \
  --project="$GOOGLE_CLOUD_PROJECT" --data-file=-
```

---

## 4. Deploy with ADK CLI, Agent CLI, or Vertex AI Agent Engine

### Option A — Vertex AI Agent Engine (Managed ADK Runtime + Memory Bank)

```bash
# Dry-run preview of deployment spec & Secret Manager references
python -m deployment.deploy_agent_engine \
  --project "$GOOGLE_CLOUD_PROJECT" \
  --location "$GOOGLE_CLOUD_LOCATION" \
  --staging-bucket "gs://${GOOGLE_CLOUD_PROJECT}-sentinel-sre-agent-artifacts" \
  --dry-run

# Live deployment to Vertex AI Agent Engine
make deploy-agent-engine
```

Or via `agents-cli` (from Agent Starter Pack / Google Agent CLI):

```bash
agents-cli deploy \
  --project "$GOOGLE_CLOUD_PROJECT" \
  --region "$GOOGLE_CLOUD_LOCATION" \
  --secrets "SENTINEL_PAGERDUTY_API_KEY=SENTINEL_PAGERDUTY_API_KEY,SENTINEL_DEPLOY_API_TOKEN=SENTINEL_DEPLOY_API_TOKEN"
```

### Option B — Cloud Run (Containerized FastAPI + OpenTelemetry Cloud Trace)

```bash
# Build & push container to Artifact Registry
gcloud builds submit \
  --tag "${GOOGLE_CLOUD_LOCATION}-docker.pkg.dev/${GOOGLE_CLOUD_PROJECT}/sentinel-sre-agent/sentinel-sre-agent:latest"

# Deploy with ADK CLI and Cloud Trace enabled
make deploy-cloud-run
```

---

## 5. Automated CI/CD & Evaluation Gate

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) enforces:
1. `ruff check` & `mypy` static quality checks
2. `pytest` unit, integration, and golden evaluation regression tests (including static scans guaranteeing zero hardcoded secrets and zero raw `print()` statements)
3. `terraform fmt -check` & `terraform validate`
4. Automated live `adk eval` gate + Vertex AI Agent Engine rollout on `main` when GCP Workload Identity Federation variables are configured.
