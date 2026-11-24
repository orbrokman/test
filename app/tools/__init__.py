"""Diagnostic and HITL-protected remediation tools for Sentinel SRE Agent."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "KNOWN_SERVICES",
    "PersistentRunbookVectorStore",
    "build_guided_error",
    "build_validation_error",
    "build_vertex_ai_search_tool",
    "create_critical_incident_ticket",
    "create_critical_incident_ticket_tool",
    "fetch_service_error_metrics",
    "inspect_service_dependency_topology",
    "query_recent_deployment_changes",
    "requires_paging_confirmation",
    "requires_scale_confirmation",
    "rollback_production_deployment",
    "rollback_production_deployment_tool",
    "scale_cloud_run_replicas",
    "scale_cloud_run_replicas_tool",
    "search_incident_runbooks_vector_store",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "KNOWN_SERVICES": ("app.tools.diagnostics_tools", "KNOWN_SERVICES"),
    "fetch_service_error_metrics": (
        "app.tools.diagnostics_tools",
        "fetch_service_error_metrics",
    ),
    "inspect_service_dependency_topology": (
        "app.tools.diagnostics_tools",
        "inspect_service_dependency_topology",
    ),
    "query_recent_deployment_changes": (
        "app.tools.diagnostics_tools",
        "query_recent_deployment_changes",
    ),
    "search_incident_runbooks_vector_store": (
        "app.tools.diagnostics_tools",
        "search_incident_runbooks_vector_store",
    ),
    "build_guided_error": ("app.tools.errors", "build_guided_error"),
    "build_validation_error": ("app.tools.errors", "build_validation_error"),
    "PersistentRunbookVectorStore": (
        "app.tools.knowledge_store",
        "PersistentRunbookVectorStore",
    ),
    "build_vertex_ai_search_tool": (
        "app.tools.knowledge_store",
        "build_vertex_ai_search_tool",
    ),
    "create_critical_incident_ticket": (
        "app.tools.remediation_tools",
        "create_critical_incident_ticket",
    ),
    "create_critical_incident_ticket_tool": (
        "app.tools.remediation_tools",
        "create_critical_incident_ticket_tool",
    ),
    "requires_paging_confirmation": (
        "app.tools.remediation_tools",
        "requires_paging_confirmation",
    ),
    "requires_scale_confirmation": (
        "app.tools.remediation_tools",
        "requires_scale_confirmation",
    ),
    "rollback_production_deployment": (
        "app.tools.remediation_tools",
        "rollback_production_deployment",
    ),
    "rollback_production_deployment_tool": (
        "app.tools.remediation_tools",
        "rollback_production_deployment_tool",
    ),
    "scale_cloud_run_replicas": (
        "app.tools.remediation_tools",
        "scale_cloud_run_replicas",
    ),
    "scale_cloud_run_replicas_tool": (
        "app.tools.remediation_tools",
        "scale_cloud_run_replicas_tool",
    ),
}


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module_name, attr_name = _EXPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
