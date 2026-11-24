"""Unit tests verifying Human-in-the-Loop (HITL) confirmation stops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.tools.remediation_tools import (
    create_critical_incident_ticket,
    requires_paging_confirmation,
    requires_scale_confirmation,
    rollback_production_deployment,
    rollback_production_deployment_tool,
    scale_cloud_run_replicas,
)


@dataclass
class _FakeConfirmation:
    confirmed: bool
    payload: dict[str, Any] | None = None


class _FakeToolContext:
    def __init__(self, confirmation: _FakeConfirmation | None = None) -> None:
        self.tool_confirmation = confirmation
        self.function_call_id = "call-hitl-001"
        self.agent_name = "remediation_executor_agent"
        self.requested: list[dict[str, Any]] = []

    def request_confirmation(
        self, *, hint: str | None = None, payload: Any | None = None
    ) -> None:
        self.requested.append({"hint": hint, "payload": payload})


def test_rollback_pauses_without_human_confirmation_and_requests_approval() -> None:
    ctx = _FakeToolContext(confirmation=None)
    result = rollback_production_deployment(
        service_name="checkout-api",
        environment="production",
        target_revision_id="rev-2026-11-20-084",
        justification="Mitigate SEV1 Cloud SQL connection pool exhaustion.",
        incident_id="INC-10421",
        tool_context=ctx,
    )
    assert result["status"] == "awaiting_human_confirmation"
    assert result["human_approved"] is False
    assert len(ctx.requested) == 1
    assert "HUMAN APPROVAL REQUIRED" in ctx.requested[0]["hint"]
    assert getattr(rollback_production_deployment_tool, "_require_confirmation", True) is True


def test_rollback_respects_human_rejection() -> None:
    ctx = _FakeToolContext(confirmation=_FakeConfirmation(confirmed=False))
    result = rollback_production_deployment(
        service_name="checkout-api",
        environment="production",
        target_revision_id="rev-2026-11-20-084",
        justification="Mitigate SEV1 Cloud SQL connection pool exhaustion.",
        incident_id="INC-10421",
        tool_context=ctx,
    )
    assert result["status"] == "rejected"
    assert result["human_approved"] is False


def test_rollback_executes_only_after_explicit_human_approval() -> None:
    ctx = _FakeToolContext(confirmation=_FakeConfirmation(confirmed=True))
    result = rollback_production_deployment(
        service_name="checkout-api",
        environment="production",
        target_revision_id="rev-2026-11-20-084",
        justification="Mitigate SEV1 Cloud SQL connection pool exhaustion.",
        incident_id="INC-10421",
        tool_context=ctx,
    )
    assert result["status"] == "success"
    assert result["human_approved"] is True
    assert result["target_revision_id"] == "rev-2026-11-20-084"


def test_conditional_hitl_for_scaling_and_paging() -> None:
    # Production scaling requires confirmation
    assert requires_scale_confirmation("payments-gateway", "production", 8) is True
    # Staging scaling <= 10 can run immediately without HITL pause
    assert requires_scale_confirmation("payments-gateway", "staging", 6) is False
    # Staging scaling > 10 requires confirmation due to cost threshold
    assert requires_scale_confirmation("payments-gateway", "staging", 25) is True

    staging_ok = scale_cloud_run_replicas(
        service_name="payments-gateway",
        environment="staging",
        target_replicas=6,
        justification="Load testing replica bump in staging.",
    )
    assert staging_ok["status"] == "success"

    prod_paused = scale_cloud_run_replicas(
        service_name="payments-gateway",
        environment="production",
        target_replicas=12,
        justification="Mitigate payment webhook traffic surge.",
        tool_context=_FakeToolContext(confirmation=None),
    )
    assert prod_paused["status"] == "awaiting_human_confirmation"

    # SEV1/SEV2 paging requires HITL; SEV3 does not
    assert requires_paging_confirmation("checkout-api", "SEV1") is True
    assert requires_paging_confirmation("search-indexer", "SEV3") is False

    sev1_paused = create_critical_incident_ticket(
        service_name="checkout-api",
        severity="SEV1",
        title="Checkout API 503 outage after release",
        summary="Production checkout-api is failing 14.8% of requests due to DB pool exhaustion.",
        recommended_actions=["Roll back to rev-2026-11-20-084"],
        tool_context=_FakeToolContext(confirmation=None),
    )
    assert sev1_paused["status"] == "awaiting_human_confirmation"
