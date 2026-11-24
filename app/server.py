"""Production FastAPI server entrypoint for Cloud Run / GKE deployments.

Uses ADK's ``get_fast_api_app`` with:
- Persistent session backend (``SESSION_DB_URL`` / Vertex AI Agent Engine)
- OpenTelemetry Cloud Trace export (``otel_to_cloud=True`` when configured)
- Structured JSON logging and health/schema introspection endpoints
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from google.adk.cli.fast_api import get_fast_api_app

from app.config import AppSettings, get_settings
from app.observability.logging import configure_logging, get_logger
from app.observability.tracing import configure_tracing
from app.schemas.models import export_all_tool_json_schemas

logger = get_logger(__name__)


def build_fastapi_server(settings: AppSettings | None = None) -> FastAPI:
    """Construct the hardened FastAPI application serving Sentinel SRE Agent."""
    cfg = settings or get_settings()
    configure_logging(cfg)
    configure_tracing(cfg)

    project_root = str(Path(__file__).resolve().parent.parent)
    session_uri: str | None = None
    if cfg.session_backend == "vertex_ai" and cfg.agent_engine_id:
        session_uri = f"agentengine://{cfg.agent_engine_id}"
    elif cfg.session_backend in ("database", "sqlite"):
        session_uri = cfg.session_db_url

    memory_uri: str | None = None
    if cfg.memory_backend == "vertex_ai_memory_bank" and cfg.agent_engine_id:
        memory_uri = f"agentengine://{cfg.agent_engine_id}"

    web_enabled = os.getenv("ADK_WEB_UI", "false").lower() == "true"

    server: FastAPI = get_fast_api_app(
        agents_dir=project_root,
        session_service_uri=session_uri,
        memory_service_uri=memory_uri,
        web=web_enabled,
        otel_to_cloud=cfg.observability.enable_cloud_trace,
        trace_to_cloud=cfg.observability.enable_cloud_trace,
        auto_create_session=True,
    )

    @server.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": cfg.observability.service_name,
            "version": cfg.observability.service_version,
            "environment": cfg.environment,
            "fast_model": cfg.models.fast_model,
            "reasoning_model": cfg.models.reasoning_model,
        }

    @server.get("/schemas")
    async def schemas() -> dict[str, Any]:
        return {"schemas": export_all_tool_json_schemas()}

    logger.info(
        "fastapi_server_initialized",
        environment=cfg.environment,
        otel_to_cloud=cfg.observability.enable_cloud_trace,
        session_backend=cfg.session_backend,
        memory_backend=cfg.memory_backend,
    )
    return server


app = build_fastapi_server()
