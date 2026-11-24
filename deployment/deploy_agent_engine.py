"""Programmatic deployment entrypoint for Vertex AI Agent Engine.

Deploys the Sentinel SRE ADK ``App`` to Vertex AI Agent Engine with:
- OpenTelemetry Cloud Trace enabled (``enable_tracing=True``)
- Secret Manager references (zero plaintext keys)
- Managed Vertex AI Sessions and Memory Bank configuration

Usage:
    python -m deployment.deploy_agent_engine \
        --project "$GOOGLE_CLOUD_PROJECT" \
        --location us-central1 \
        --staging-bucket "gs://$GOOGLE_CLOUD_PROJECT-sentinel-sre-agent-artifacts"
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.observability.logging import configure_logging, get_logger

logger = get_logger(__name__)


def build_agent_engine_spec(
    *,
    project: str,
    location: str,
    staging_bucket: str,
    display_name: str,
    service_account: str | None = None,
) -> dict[str, Any]:
    """Build the declarative deployment payload for Vertex AI Agent Engine."""
    env_vars: dict[str, Any] = {
        "SENTINEL_ENV": "production",
        "GOOGLE_CLOUD_PROJECT": project,
        "GOOGLE_CLOUD_LOCATION": location,
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "SESSION_BACKEND": "vertex_ai",
        "MEMORY_BACKEND": "vertex_ai_memory_bank",
        "ENABLE_CLOUD_TRACE": "true",
        "ENABLE_CLOUD_DLP": "true",
        "ENABLE_MODEL_ARMOR": "true",
        "FAIL_CLOSED_SECRETS": "true",
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "NO_CONTENT",
        # Secret Manager references — never plaintext keys
        "SENTINEL_PAGERDUTY_API_KEY": {
            "secret": "SENTINEL_PAGERDUTY_API_KEY",
            "version": "latest",
        },
        "SENTINEL_DEPLOY_API_TOKEN": {
            "secret": "SENTINEL_DEPLOY_API_TOKEN",
            "version": "latest",
        },
    }
    spec: dict[str, Any] = {
        "display_name": display_name,
        "description": "Sentinel Enterprise SRE Multi-Agent ADK Copilot.",
        "staging_bucket": staging_bucket,
        "requirements": [
            "google-adk>=1.14.0",
            "google-cloud-aiplatform[adk,agent_engines]>=1.112.0",
            "google-cloud-dlp>=3.25.0",
            "google-cloud-modelarmor>=0.2.0",
            "google-cloud-secret-manager>=2.21.0",
            "opentelemetry-exporter-gcp-trace>=1.7.0",
            "pydantic>=2.9.0",
            "structlog>=24.4.0",
        ],
        "extra_packages": ["./app"],
        "env_vars": env_vars,
    }
    if service_account:
        spec["service_account"] = service_account
    return spec


def deploy_to_agent_engine(
    *,
    project: str,
    location: str,
    staging_bucket: str,
    display_name: str = "sentinel-sre-agent",
    service_account: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Deploy Sentinel SRE Agent to Vertex AI Agent Engine using ``AdkApp``."""
    configure_logging()
    spec = build_agent_engine_spec(
        project=project,
        location=location,
        staging_bucket=staging_bucket,
        display_name=display_name,
        service_account=service_account,
    )
    if dry_run:
        logger.info("agent_engine_deploy_dry_run", display_name=display_name)
        return {"status": "dry_run", "spec": spec}

    import vertexai
    from vertexai import agent_engines
    from vertexai.preview.reasoning_engines import AdkApp

    from app.agent import app as adk_app_instance

    vertexai.init(
        project=project,
        location=location,
        staging_bucket=staging_bucket,
    )
    adk_wrapper = AdkApp(app=adk_app_instance, enable_tracing=True)
    remote_agent = agent_engines.create(
        agent_engine=adk_wrapper,
        display_name=spec["display_name"],
        description=spec["description"],
        requirements=spec["requirements"],
        extra_packages=spec["extra_packages"],
        env_vars=spec["env_vars"],
    )
    metadata = {
        "resource_name": remote_agent.resource_name,
        "display_name": display_name,
        "project": project,
        "location": location,
    }
    Path("deployment_metadata.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    logger.info("agent_engine_deploy_succeeded", **metadata)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy Sentinel SRE Agent to Vertex AI Agent Engine."
    )
    parser.add_argument("--project", required=True, help="GCP project ID.")
    parser.add_argument("--location", default="us-central1", help="GCP region.")
    parser.add_argument(
        "--staging-bucket",
        required=True,
        help="GCS bucket URI (gs://...) for staging packages.",
    )
    parser.add_argument(
        "--display-name",
        default="sentinel-sre-agent",
        help="Display name on Vertex AI Agent Engine.",
    )
    parser.add_argument(
        "--service-account",
        default=None,
        help="Optional runtime service account email.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print validated deployment spec without creating cloud resources.",
    )
    args = parser.parse_args()
    result = deploy_to_agent_engine(
        project=args.project,
        location=args.location,
        staging_bucket=args.staging_bucket,
        display_name=args.display_name,
        service_account=args.service_account,
        dry_run=args.dry_run,
    )
    logger.info("deploy_cli_finished", status=result.get("status", "deployed"))


if __name__ == "__main__":
    main()
