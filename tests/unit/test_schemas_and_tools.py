"""Unit tests verifying Tool Docstrings, Descriptive Naming, JSON Schemas, & Guided Errors."""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.schemas.models import (
    ServiceMetricsInput,
    export_all_tool_json_schemas,
)
from app.tools.diagnostics_tools import (
    fetch_service_error_metrics,
    inspect_service_dependency_topology,
    query_recent_deployment_changes,
    search_incident_runbooks_vector_store,
)
from app.tools.remediation_tools import (
    create_critical_incident_ticket,
    rollback_production_deployment,
    scale_cloud_run_replicas,
)

ALL_TOOLS = (
    fetch_service_error_metrics,
    query_recent_deployment_changes,
    search_incident_runbooks_vector_store,
    inspect_service_dependency_topology,
    rollback_production_deployment,
    scale_cloud_run_replicas,
    create_critical_incident_ticket,
)


def test_every_tool_has_descriptive_name_and_comprehensive_docstring() -> None:
    for fn in ALL_TOOLS:
        name = fn.__name__
        doc = inspect.getdoc(fn) or ""
        assert "_" in name and len(name.split("_")) >= 3, (
            f"Tool name '{name}' should be specific and descriptive."
        )
        assert "Args:" in doc and "Returns:" in doc, (
            f"Tool '{name}' must document all arguments and return contract."
        )
        sig = inspect.signature(fn)
        for param_name in sig.parameters:
            if param_name == "tool_context":
                continue
            assert param_name in doc, (
                f"Parameter '{param_name}' missing from docstring of '{name}'."
            )


def test_strict_json_schemas_reject_extra_fields_and_export_cleanly() -> None:
    schemas = export_all_tool_json_schemas()
    assert "ServiceMetricsInput" in schemas
    assert "RollbackDeploymentInput" in schemas
    assert "RemediationPlan" in schemas
    assert "CriticEvaluationReport" in schemas
    assert schemas["ServiceMetricsInput"]["additionalProperties"] is False

    with pytest.raises(ValidationError):
        ServiceMetricsInput(
            service_name="checkout-api",
            environment="production",
            window_minutes=30,
            unexpected_key="forbidden",  # type: ignore[call-arg]
        )


def test_diagnostic_tools_return_schema_valid_payloads() -> None:
    metrics = fetch_service_error_metrics("checkout-api", "production", 30)
    assert metrics["status"] == "success"
    assert metrics["slo_breached"] is True
    assert metrics["error_rate_percent"] > 5.0

    deploys = query_recent_deployment_changes("checkout-api", "production", 5)
    assert deploys["status"] == "success"
    assert deploys["last_known_good_revision_id"] == "rev-2026-11-20-084"

    topo = inspect_service_dependency_topology("checkout-api")
    assert topo["status"] == "success"
    assert topo["tier"] == "tier-0"
    assert topo["oncall_team"] == "sre-commerce-core"

    runbooks = search_incident_runbooks_vector_store(
        query="HTTP 503 Cloud SQL connection pool exhausted",
        service_name="checkout-api",
        top_k=2,
    )
    assert runbooks["status"] == "success"
    assert runbooks["matches"][0]["runbook_id"] == "RB-CHECKOUT-001"


def test_guided_error_handling_for_invalid_inputs_and_unknown_services() -> None:
    bad_window = fetch_service_error_metrics("checkout-api", "production", 99999)
    assert bad_window["status"] == "error"
    assert bad_window["error_code"] == "SCHEMA_VALIDATION_ERROR"
    assert len(bad_window["recovery_instructions"]) >= 1
    assert bad_window["retryable"] is True

    unknown_svc = fetch_service_error_metrics("nonexistent-service", "production", 30)
    assert unknown_svc["status"] == "error"
    assert unknown_svc["error_code"] == "UNKNOWN_SERVICE_NAME"
    assert "checkout-api" in unknown_svc["valid_examples"]
    assert unknown_svc["suggested_next_tool"] == "search_incident_runbooks_vector_store"

    bad_rev = rollback_production_deployment(
        service_name="checkout-api",
        environment="production",
        target_revision_id="rev-does-not-exist",
        justification="Testing guided error recovery for missing revision ID.",
        incident_id="INC-10421",
    )
    assert bad_rev["status"] == "error"
    assert bad_rev["error_code"] == "UNKNOWN_TARGET_REVISION"
    assert "rev-2026-11-20-084" in bad_rev["valid_examples"]
    assert bad_rev["suggested_next_tool"] == "query_recent_deployment_changes"
