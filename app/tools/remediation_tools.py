"""High-stakes SRE remediation tools protected by explicit Human-in-the-Loop hooks.

All state-mutating production operations implement defense-in-depth HITL stops:
1. Declarative ADK confirmation via ``FunctionTool(..., require_confirmation=...)``
   (unconditional for production rollbacks, conditional for replica scaling and
   SEV1/SEV2 on-call paging).
2. Programmatic confirmation inside the tool via ``tool_context.request_confirmation``
   and ``tool_context.tool_confirmation`` checking so explicit pause/approve/reject
   handling is guaranteed even when invoked directly or in custom runners.
3. Automatic PII scrubbing via ``PiiRedactor`` before storing incident tickets,
   and Secret Manager credential injection via ``SecretManagerProvider``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.schemas.models import (
    CreateIncidentTicketInput,
    CreateIncidentTicketOutput,
    RollbackDeploymentInput,
    RollbackDeploymentOutput,
    ScaleReplicasInput,
    ScaleReplicasOutput,
)
from app.security.pii_redactor import get_pii_redactor
from app.security.secrets import get_secret_provider
from app.tools.diagnostics_tools import (
    KNOWN_SERVICES,
    _DEPLOYMENT_HISTORY,
    _SERVICE_TELEMETRY,
    _SERVICE_TOPOLOGY,
)
from app.tools.errors import build_guided_error, build_validation_error

try:
    from google.adk.tools import FunctionTool, ToolContext
except ImportError:  # pragma: no cover - allows offline unit testing without ADK wheel
    ToolContext = Any  # type: ignore[assignment,misc]

    class FunctionTool:  # type: ignore[no-redef]
        def __init__(self, func: Any, *, require_confirmation: Any = False) -> None:
            self.func = func
            self.name = getattr(func, "__name__", "unknown")
            self.description = getattr(func, "__doc__", "") or ""
            self.require_confirmation = require_confirmation


def requires_scale_confirmation(
    service_name: str,
    environment: str = "production",
    target_replicas: int = 1,
    **_: Any,
) -> bool:
    """Conditional HITL policy predicate for ``scale_cloud_run_replicas``.

    Returns ``True`` (requiring human sign-off) whenever scaling touches
    ``production`` OR exceeds the configured high-cost replica threshold.
    """
    del service_name
    threshold = get_settings().security.production_scale_confirmation_threshold
    return environment == "production" or target_replicas > threshold


def requires_paging_confirmation(
    service_name: str,
    severity: str = "SEV3",
    **_: Any,
) -> bool:
    """Conditional HITL policy predicate for ``create_critical_incident_ticket``.

    Returns ``True`` for SEV1 and SEV2 tickets that page live on-call engineers.
    """
    del service_name
    return severity.upper() in {"SEV1", "SEV2"}


def _evaluate_hitl_gate(
    *,
    tool_context: ToolContext | None,
    requires_approval: bool,
    hint: str,
    payload: dict[str, Any],
) -> tuple[str, bool]:
    """Inspect ``tool_context.tool_confirmation`` and trigger HITL pause if needed.

    Returns:
        Tuple of ``(gate_state, human_approved)`` where ``gate_state`` is one of
        ``'proceed'``, ``'awaiting_human_confirmation'``, or ``'rejected'``.
    """
    if not requires_approval:
        return ("proceed", False)

    if tool_context is None:
        return ("awaiting_human_confirmation", False)

    confirmation = getattr(tool_context, "tool_confirmation", None)
    if confirmation is None:
        request_fn = getattr(tool_context, "request_confirmation", None)
        if callable(request_fn):
            try:
                request_fn(hint=hint, payload=payload)
            except ValueError:
                # function_call_id may be unset in direct non-runner unit tests.
                pass
        return ("awaiting_human_confirmation", False)

    if bool(getattr(confirmation, "confirmed", False)):
        return ("proceed", True)

    return ("rejected", False)


def rollback_production_deployment(
    service_name: str,
    environment: str,
    target_revision_id: str,
    justification: str,
    incident_id: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Roll back a microservice deployment to a verified last-known-good revision.

    HIGH-STAKES MUTATING ACTION: Always enforces an explicit Human-in-the-Loop
    (HITL) confirmation gate before mutating deployment traffic. Before calling
    this tool, the agent must verify ``target_revision_id`` using
    ``query_recent_deployment_changes`` and open or cite a valid ``incident_id``.

    Args:
        service_name: Canonical kebab-case microservice identifier (e.g.,
            'checkout-api').
        environment: Target deployment environment ('production', 'staging',
            or 'canary').
        target_revision_id: Verified historical revision ID to restore (e.g.,
            'rev-2026-11-20-084'). Cannot equal the currently failing revision.
        justification: Evidence-backed operational rationale citing observed
            metrics or runbook IDs (10 to 500 characters).
        incident_id: Active incident ticket ID matching ``INC-\\d{4,8}`` (e.g.,
            'INC-10421').

    Returns:
        A JSON-serializable dictionary conforming to ``RollbackDeploymentOutput``
        with ``status`` in ``{'awaiting_human_confirmation', 'rejected', 'success'}``
        or ``ToolErrorResponse`` with step-by-step recovery guidance if any
        precondition fails.
    """
    tool_name = "rollback_production_deployment"
    try:
        validated = RollbackDeploymentInput(
            service_name=service_name,
            environment=environment,  # type: ignore[arg-type]
            target_revision_id=target_revision_id,
            justification=justification,
            incident_id=incident_id,
        )
    except ValidationError as exc:
        return build_validation_error(
            tool_name=tool_name,
            exc=exc,
            valid_examples=[
                "service_name='checkout-api', environment='production', "
                "target_revision_id='rev-2026-11-20-084', incident_id='INC-10421'"
            ],
            suggested_next_tool="query_recent_deployment_changes",
        )

    history = _DEPLOYMENT_HISTORY.get(validated.service_name)
    if history is None:
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNKNOWN_SERVICE_NAME",
            message=f"Cannot roll back unknown service '{validated.service_name}'.",
            recovery_instructions=[
                "Verify the exact service identifier against valid_examples.",
                "Run query_recent_deployment_changes before retrying rollback.",
            ],
            suggested_next_tool="query_recent_deployment_changes",
            valid_examples=list(KNOWN_SERVICES),
            retryable=True,
        )

    known_revs = {rev["revision_id"] for rev in history["revisions"]}
    if validated.target_revision_id not in known_revs:
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNKNOWN_TARGET_REVISION",
            message=(
                f"Revision '{validated.target_revision_id}' does not exist for "
                f"service '{validated.service_name}'."
            ),
            recovery_instructions=[
                "Call query_recent_deployment_changes to inspect valid historical revisions.",
                f"Use the verified last_known_good_revision_id ('{history['last_known_good_revision_id']}').",
            ],
            suggested_next_tool="query_recent_deployment_changes",
            valid_examples=[history["last_known_good_revision_id"]],
            retryable=True,
        )

    if validated.target_revision_id == history["current_revision_id"]:
        return build_guided_error(
            tool_name=tool_name,
            error_code="NOOP_ROLLBACK_SAME_REVISION",
            message=(
                f"Target revision '{validated.target_revision_id}' is already "
                f"the active revision for '{validated.service_name}'."
            ),
            recovery_instructions=[
                f"Roll back to last_known_good_revision_id ('{history['last_known_good_revision_id']}') instead.",
            ],
            suggested_next_tool="query_recent_deployment_changes",
            valid_examples=[history["last_known_good_revision_id"]],
            retryable=True,
        )

    # Verify deploy token via Secret Manager (never hardcoded).
    secret_provider = get_secret_provider()
    secret_provider.get_secret(
        "SENTINEL_DEPLOY_API_TOKEN",
        default_for_local="local-simulated-deploy-token",
    )

    gate_state, human_approved = _evaluate_hitl_gate(
        tool_context=tool_context,
        requires_approval=True,
        hint=(
            f"HUMAN APPROVAL REQUIRED: Roll back {validated.service_name} "
            f"({validated.environment}) from {history['current_revision_id']} "
            f"to {validated.target_revision_id} under {validated.incident_id}?"
        ),
        payload={
            "action": tool_name,
            "service_name": validated.service_name,
            "environment": validated.environment,
            "previous_revision_id": history["current_revision_id"],
            "target_revision_id": validated.target_revision_id,
            "incident_id": validated.incident_id,
            "justification": validated.justification,
        },
    )

    if gate_state == "awaiting_human_confirmation":
        return RollbackDeploymentOutput(
            status="awaiting_human_confirmation",
            service_name=validated.service_name,
            environment=validated.environment,
            previous_revision_id=history["current_revision_id"],
            target_revision_id=validated.target_revision_id,
            incident_id=validated.incident_id,
            human_approved=False,
            message=(
                "Execution paused pending explicit human confirmation. Present "
                "the target revision, blast radius, and justification to the "
                "on-call engineer for approval."
            ),
        ).model_dump()

    if gate_state == "rejected":
        return RollbackDeploymentOutput(
            status="rejected",
            service_name=validated.service_name,
            environment=validated.environment,
            previous_revision_id=history["current_revision_id"],
            target_revision_id=validated.target_revision_id,
            incident_id=validated.incident_id,
            human_approved=False,
            message=(
                "Rollback was explicitly rejected by the human operator. "
                "No traffic changes were made."
            ),
        ).model_dump()

    return RollbackDeploymentOutput(
        status="success",
        service_name=validated.service_name,
        environment=validated.environment,
        previous_revision_id=history["current_revision_id"],
        target_revision_id=validated.target_revision_id,
        incident_id=validated.incident_id,
        human_approved=human_approved,
        message=(
            f"Human-approved rollback executed: shifted 100% traffic of "
            f"{validated.service_name} ({validated.environment}) to "
            f"{validated.target_revision_id}."
        ),
    ).model_dump()


def scale_cloud_run_replicas(
    service_name: str,
    environment: str,
    target_replicas: int,
    justification: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Adjust the replica count of a Cloud Run microservice to mitigate load spikes.

    CONDITIONAL HITL ACTION: Requires explicit human confirmation whenever
    ``environment == 'production'`` OR ``target_replicas`` exceeds the
    configured cost/blast-radius threshold (default 10). Non-production scaling
    within safe bounds can execute immediately.

    Args:
        service_name: Canonical kebab-case Cloud Run service name (e.g.,
            'payments-gateway').
        environment: Target environment ('production', 'staging', or 'canary').
        target_replicas: Desired replica count (integer between 1 and 100).
        justification: Evidence-backed operational justification (10 to 500 chars).

    Returns:
        A JSON-serializable dictionary conforming to ``ScaleReplicasOutput`` with
        cost delta estimate and approval status, or ``ToolErrorResponse`` on
        invalid inputs.
    """
    tool_name = "scale_cloud_run_replicas"
    try:
        validated = ScaleReplicasInput(
            service_name=service_name,
            environment=environment,  # type: ignore[arg-type]
            target_replicas=target_replicas,
            justification=justification,
        )
    except ValidationError as exc:
        return build_validation_error(
            tool_name=tool_name,
            exc=exc,
            valid_examples=[
                "service_name='payments-gateway', environment='production', target_replicas=12"
            ],
        )

    telemetry = _SERVICE_TELEMETRY.get(validated.service_name)
    if telemetry is None:
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNKNOWN_SERVICE_NAME",
            message=f"Cloud Run service '{validated.service_name}' not found.",
            recovery_instructions=[
                "Pick a valid service from valid_examples.",
                "Verify active replica counts via fetch_service_error_metrics before scaling.",
            ],
            suggested_next_tool="fetch_service_error_metrics",
            valid_examples=list(KNOWN_SERVICES),
            retryable=True,
        )

    current_replicas = int(telemetry["active_replicas"])
    hourly_delta = round((validated.target_replicas - current_replicas) * 0.42, 2)
    needs_hitl = requires_scale_confirmation(
        service_name=validated.service_name,
        environment=validated.environment,
        target_replicas=validated.target_replicas,
    )

    gate_state, human_approved = _evaluate_hitl_gate(
        tool_context=tool_context,
        requires_approval=needs_hitl,
        hint=(
            f"HUMAN APPROVAL REQUIRED: Scale {validated.service_name} "
            f"({validated.environment}) from {current_replicas} to "
            f"{validated.target_replicas} replicas (est. ${hourly_delta:+.2f}/hr)?"
        ),
        payload={
            "action": tool_name,
            "service_name": validated.service_name,
            "environment": validated.environment,
            "previous_replicas": current_replicas,
            "target_replicas": validated.target_replicas,
            "estimated_hourly_delta_usd": hourly_delta,
        },
    )

    if gate_state == "awaiting_human_confirmation":
        return ScaleReplicasOutput(
            status="awaiting_human_confirmation",
            service_name=validated.service_name,
            environment=validated.environment,
            previous_replicas=current_replicas,
            target_replicas=validated.target_replicas,
            human_approved=False,
            estimated_hourly_delta_usd=hourly_delta,
            message=(
                "Scaling paused for human-in-the-loop approval due to production "
                "environment or high replica count policy."
            ),
        ).model_dump()

    if gate_state == "rejected":
        return ScaleReplicasOutput(
            status="rejected",
            service_name=validated.service_name,
            environment=validated.environment,
            previous_replicas=current_replicas,
            target_replicas=validated.target_replicas,
            human_approved=False,
            estimated_hourly_delta_usd=hourly_delta,
            message="Scaling request rejected by human operator. Replicas unchanged.",
        ).model_dump()

    return ScaleReplicasOutput(
        status="success",
        service_name=validated.service_name,
        environment=validated.environment,
        previous_replicas=current_replicas,
        target_replicas=validated.target_replicas,
        human_approved=human_approved,
        estimated_hourly_delta_usd=hourly_delta,
        message=(
            f"Scaled {validated.service_name} ({validated.environment}) from "
            f"{current_replicas} to {validated.target_replicas} replicas."
        ),
    ).model_dump()


def create_critical_incident_ticket(
    service_name: str,
    severity: str,
    title: str,
    summary: str,
    recommended_actions: list[str],
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Create a structured incident ticket and optionally page the owning on-call team.

    Automatically scrubs all PII (emails, SSNs, phones, credit cards, API keys)
    from ``title`` and ``summary`` before storage. Requires explicit HITL
    confirmation for ``SEV1`` and ``SEV2`` incidents that page live responders.
    Retrieves ticketing credentials securely from Secret Manager.

    Args:
        service_name: Primary affected kebab-case service name.
        severity: Incident severity ('SEV1', 'SEV2', 'SEV3', or 'SEV4').
        title: Concise incident headline (10 to 160 characters).
        summary: Detailed diagnostic and customer-impact summary (20 to 2000 chars).
        recommended_actions: Ordered list of 1 to 10 mitigation steps.

    Returns:
        A JSON-serializable dictionary conforming to ``CreateIncidentTicketOutput``
        with generated ``incident_id``, ``paged_oncall_team``, ``pii_redacted``
        flag, and approval state, or ``ToolErrorResponse`` on validation failure.
    """
    tool_name = "create_critical_incident_ticket"
    try:
        validated = CreateIncidentTicketInput(
            service_name=service_name,
            severity=severity.upper(),  # type: ignore[arg-type]
            title=title,
            summary=summary,
            recommended_actions=recommended_actions,
        )
    except ValidationError as exc:
        return build_validation_error(
            tool_name=tool_name,
            exc=exc,
            valid_examples=[
                "service_name='checkout-api', severity='SEV1', "
                "title='Checkout API 503 outage due to DB pool exhaustion'"
            ],
        )

    topo = _SERVICE_TOPOLOGY.get(validated.service_name)
    if topo is None:
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNKNOWN_SERVICE_NAME",
            message=f"Cannot route incident ticket for unknown service '{validated.service_name}'.",
            recovery_instructions=[
                "Provide a registered service name from valid_examples.",
                "Use inspect_service_dependency_topology to verify on-call team ownership.",
            ],
            suggested_next_tool="inspect_service_dependency_topology",
            valid_examples=list(KNOWN_SERVICES),
            retryable=True,
        )

    # Actively redact PII before ticket persistence or paging.
    redactor = get_pii_redactor()
    clean_title, title_redacted = redactor.redact_text(validated.title)
    clean_summary, summary_redacted = redactor.redact_text(validated.summary)
    pii_was_redacted = title_redacted or summary_redacted

    # Retrieve ticketing API credential securely via Secret Manager.
    secret_provider = get_secret_provider()
    secret_provider.get_secret(
        "SENTINEL_PAGERDUTY_API_KEY",
        default_for_local="local-simulated-pagerduty-key",
    )

    digest = hashlib.sha256(
        f"{validated.service_name}:{validated.severity}:{clean_title}".encode("utf-8")
    ).hexdigest()
    incident_id = f"INC-{int(digest[:6], 16) % 90000 + 10000}"

    needs_hitl = requires_paging_confirmation(
        service_name=validated.service_name,
        severity=validated.severity,
    )
    gate_state, human_approved = _evaluate_hitl_gate(
        tool_context=tool_context,
        requires_approval=needs_hitl,
        hint=(
            f"HUMAN APPROVAL REQUIRED: Open {validated.severity} incident "
            f"{incident_id} and page on-call team '{topo['oncall_team']}'?"
        ),
        payload={
            "action": tool_name,
            "incident_id": incident_id,
            "service_name": validated.service_name,
            "severity": validated.severity,
            "title": clean_title,
            "oncall_team": topo["oncall_team"],
        },
    )

    if gate_state == "awaiting_human_confirmation":
        return CreateIncidentTicketOutput(
            status="awaiting_human_confirmation",
            incident_id=incident_id,
            service_name=validated.service_name,
            severity=validated.severity,
            title=clean_title,
            paged_oncall_team=topo["oncall_team"],
            pii_redacted=pii_was_redacted,
            human_approved=False,
            message=(
                f"Paging paused pending human approval for {validated.severity} "
                f"incident {incident_id} (team: {topo['oncall_team']})."
            ),
        ).model_dump()

    if gate_state == "rejected":
        return CreateIncidentTicketOutput(
            status="rejected",
            incident_id=incident_id,
            service_name=validated.service_name,
            severity=validated.severity,
            title=clean_title,
            paged_oncall_team=topo["oncall_team"],
            pii_redacted=pii_was_redacted,
            human_approved=False,
            message="Incident paging rejected by human operator. Ticket was not filed.",
        ).model_dump()

    return CreateIncidentTicketOutput(
        status="success",
        incident_id=incident_id,
        service_name=validated.service_name,
        severity=validated.severity,
        title=clean_title,
        paged_oncall_team=topo["oncall_team"],
        pii_redacted=pii_was_redacted,
        human_approved=human_approved,
        message=(
            f"Created {validated.severity} ticket {incident_id} for "
            f"{validated.service_name} (on-call: {topo['oncall_team']}). "
            f"Summary stored safely ({len(clean_summary)} chars)."
        ),
    ).model_dump()


# ADK FunctionTool wrappers declaring explicit HITL confirmation gates.
rollback_production_deployment_tool = FunctionTool(
    func=rollback_production_deployment,
    require_confirmation=True,
)
scale_cloud_run_replicas_tool = FunctionTool(
    func=scale_cloud_run_replicas,
    require_confirmation=requires_scale_confirmation,
)
create_critical_incident_ticket_tool = FunctionTool(
    func=create_critical_incident_ticket,
    require_confirmation=requires_paging_confirmation,
)
