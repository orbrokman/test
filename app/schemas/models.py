"""Strict Pydantic v2 input and output JSON schemas for tools and sub-agents.

Every tool validates incoming LLM arguments against an explicit schema with
``extra="forbid"`` and returns a schema-validated JSON payload. Specialist
sub-agents also declare ``output_schema`` using these models to constrain
structured reasoning outputs and enable deterministic downstream routing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SeverityLevel = Literal["SEV1", "SEV2", "SEV3", "SEV4"]
DeploymentEnvironment = Literal["production", "staging", "canary"]
ToolStatus = Literal["success", "error", "awaiting_human_confirmation", "rejected"]


class StrictSchema(BaseModel):
    """Base schema rejecting unknown keys and stripping surrounding whitespace."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


# ---------------------------------------------------------------------------
# Guided Error Schema
# ---------------------------------------------------------------------------


class ToolErrorResponse(StrictSchema):
    """Structured error response providing guided recovery instructions to LLMs."""

    status: Literal["error"] = "error"
    tool_name: str = Field(
        description="Exact name of the tool that encountered the error."
    )
    error_code: str = Field(
        description="Machine-readable error code (e.g., INVALID_SERVICE_ID)."
    )
    message: str = Field(
        description="Human- and LLM-readable summary of what went wrong."
    )
    recovery_instructions: list[str] = Field(
        min_length=1,
        description="Actionable step-by-step instructions telling the agent how to recover.",
    )
    suggested_next_tool: str | None = Field(
        default=None,
        description="Optional recommended tool name to call next to resolve the issue.",
    )
    valid_examples: list[str] = Field(
        default_factory=list,
        description="Optional valid parameter values to guide self-correction.",
    )
    retryable: bool = Field(
        default=True,
        description="Whether retrying with corrected parameters may succeed.",
    )


# ---------------------------------------------------------------------------
# Diagnostic Tool Input / Output Schemas
# ---------------------------------------------------------------------------


class ServiceMetricsInput(StrictSchema):
    """Validated input schema for ``fetch_service_error_metrics``."""

    service_name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Kebab-case microservice identifier (e.g., 'checkout-api').",
    )
    environment: DeploymentEnvironment = Field(
        default="production",
        description="Target deployment environment to inspect.",
    )
    window_minutes: int = Field(
        default=30,
        ge=5,
        le=1440,
        description="Lookback time window in minutes (between 5 and 1440).",
    )


class ServiceMetricsOutput(StrictSchema):
    """Structured output schema for ``fetch_service_error_metrics``."""

    status: Literal["success"] = "success"
    service_name: str
    environment: DeploymentEnvironment
    window_minutes: int
    error_rate_percent: float = Field(ge=0.0, le=100.0)
    p99_latency_ms: float = Field(ge=0.0)
    requests_per_second: float = Field(ge=0.0)
    active_replicas: int = Field(ge=0)
    top_error_signatures: list[str]
    slo_breached: bool
    observed_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class DeploymentHistoryInput(StrictSchema):
    """Validated input schema for ``query_recent_deployment_changes``."""

    service_name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Kebab-case microservice identifier.",
    )
    environment: DeploymentEnvironment = Field(
        default="production",
        description="Target deployment environment.",
    )
    limit: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Maximum number of recent revisions to return (1-20).",
    )


class DeploymentRecord(StrictSchema):
    """Single immutable deployment revision record."""

    revision_id: str
    deployed_at_utc: str
    commit_sha: str
    deployed_by_role: str
    change_summary: str
    health_status: Literal["healthy", "degraded", "rolled_back"]
    is_current: bool


class DeploymentHistoryOutput(StrictSchema):
    """Structured output schema for ``query_recent_deployment_changes``."""

    status: Literal["success"] = "success"
    service_name: str
    environment: DeploymentEnvironment
    current_revision_id: str
    last_known_good_revision_id: str
    revisions: list[DeploymentRecord]


class RunbookSearchInput(StrictSchema):
    """Validated input schema for ``search_incident_runbooks_vector_store``."""

    query: str = Field(
        min_length=3,
        max_length=500,
        description="Natural-language symptom, error signature, or service query.",
    )
    service_name: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Optional service filter to narrow vector similarity search.",
    )
    top_k: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Number of top matching runbooks to return (1-10).",
    )


class RunbookHit(StrictSchema):
    """Single runbook match retrieved from the persistent vector store."""

    runbook_id: str
    title: str
    service_name: str
    similarity_score: float = Field(ge=0.0, le=1.0)
    root_cause_pattern: str
    mitigation_steps: list[str]
    requires_human_approval: bool


class RunbookSearchOutput(StrictSchema):
    """Structured output schema for ``search_incident_runbooks_vector_store``."""

    status: Literal["success"] = "success"
    query: str
    store_backend: str
    matches: list[RunbookHit]


class DependencyTopologyInput(StrictSchema):
    """Validated input schema for ``inspect_service_dependency_topology``."""

    service_name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Service whose upstream/downstream blast radius to inspect.",
    )


class DependencyTopologyOutput(StrictSchema):
    """Structured output schema for ``inspect_service_dependency_topology``."""

    status: Literal["success"] = "success"
    service_name: str
    tier: Literal["tier-0", "tier-1", "tier-2"]
    upstream_callers: list[str]
    downstream_dependencies: list[str]
    blast_radius_summary: str
    oncall_team: str


# ---------------------------------------------------------------------------
# High-Stakes Remediation Tool Input / Output Schemas (HITL-Protected)
# ---------------------------------------------------------------------------


class RollbackDeploymentInput(StrictSchema):
    """Validated input schema for ``rollback_production_deployment``."""

    service_name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Target microservice identifier to roll back.",
    )
    environment: DeploymentEnvironment = Field(
        default="production",
        description="Environment to roll back (production requires human confirmation).",
    )
    target_revision_id: str = Field(
        min_length=4,
        max_length=64,
        pattern=r"^[a-zA-Z0-9._-]+$",
        description="Last-known-good revision ID to restore (e.g., 'rev-2026-11-20-042').",
    )
    justification: str = Field(
        min_length=10,
        max_length=500,
        description="Evidence-backed operational justification referencing observed metrics/logs.",
    )
    incident_id: str = Field(
        pattern=r"^INC-\d{4,8}$",
        description="Active incident ticket ID (format: INC-12345).",
    )


class RollbackDeploymentOutput(StrictSchema):
    """Structured output schema for ``rollback_production_deployment``."""

    status: ToolStatus
    service_name: str
    environment: DeploymentEnvironment
    previous_revision_id: str
    target_revision_id: str
    incident_id: str
    human_approved: bool
    message: str
    executed_at_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ScaleReplicasInput(StrictSchema):
    """Validated input schema for ``scale_cloud_run_replicas``."""

    service_name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Cloud Run service identifier to scale.",
    )
    environment: DeploymentEnvironment = Field(
        default="production",
        description="Deployment environment for the service.",
    )
    target_replicas: int = Field(
        ge=1,
        le=100,
        description="Desired replica count (1-100; values > 10 or production require HITL approval).",
    )
    justification: str = Field(
        min_length=10,
        max_length=500,
        description="Operational reason for the scaling adjustment.",
    )


class ScaleReplicasOutput(StrictSchema):
    """Structured output schema for ``scale_cloud_run_replicas``."""

    status: ToolStatus
    service_name: str
    environment: DeploymentEnvironment
    previous_replicas: int
    target_replicas: int
    human_approved: bool
    estimated_hourly_delta_usd: float
    message: str


class CreateIncidentTicketInput(StrictSchema):
    """Validated input schema for ``create_critical_incident_ticket``."""

    service_name: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9-]{1,63}$",
        description="Primary affected service identifier.",
    )
    severity: SeverityLevel = Field(
        description="Incident severity classification (SEV1 through SEV4)."
    )
    title: str = Field(
        min_length=10,
        max_length=160,
        description="Concise incident headline (PII-free).",
    )
    summary: str = Field(
        min_length=20,
        max_length=2000,
        description="Detailed impact and diagnostic summary (automatically scrubbed for PII).",
    )
    recommended_actions: list[str] = Field(
        min_length=1,
        max_length=10,
        description="Ordered list of recommended mitigation actions.",
    )

    @field_validator("recommended_actions")
    @classmethod
    def _validate_non_empty_steps(cls, steps: list[str]) -> list[str]:
        cleaned = [step.strip() for step in steps if step.strip()]
        if not cleaned:
            raise ValueError("recommended_actions must contain at least one non-empty step.")
        return cleaned


class CreateIncidentTicketOutput(StrictSchema):
    """Structured output schema for ``create_critical_incident_ticket``."""

    status: ToolStatus
    incident_id: str
    service_name: str
    severity: SeverityLevel
    title: str
    paged_oncall_team: str
    pii_redacted: bool
    human_approved: bool
    message: str


# ---------------------------------------------------------------------------
# Structured Multi-Agent Sub-Agent Output Schemas
# ---------------------------------------------------------------------------


class IncidentTriageReport(StrictSchema):
    """Structured output emitted by ``incident_triage_agent``."""

    service_name: str = Field(
        description="Primary affected service identified from the user report."
    )
    environment: DeploymentEnvironment = Field(
        default="production",
        description="Affected deployment environment.",
    )
    severity: SeverityLevel = Field(
        description="Assessed severity level (SEV1-SEV4)."
    )
    symptoms: list[str] = Field(
        description="Normalized list of observed symptoms."
    )
    requires_deep_reasoning: bool = Field(
        description="True when multi-signal root-cause synthesis requires Gemini Pro."
    )
    triage_rationale: str = Field(
        description="Short explanation justifying the severity classification."
    )


class RemediationPlan(StrictSchema):
    """Structured remediation plan generated by ``root_cause_planner_agent``."""

    service_name: str
    probable_root_cause: str = Field(
        description="Primary root cause grounded in metrics, deployments, and runbooks."
    )
    confidence_score: float = Field(
        ge=0.0,
        le=1.0,
        description="Confidence in the root cause hypothesis (0.0 to 1.0).",
    )
    cited_evidence: list[str] = Field(
        min_length=1,
        description="Specific metrics, revision IDs, or runbook IDs supporting the diagnosis.",
    )
    recommended_actions: list[str] = Field(
        min_length=1,
        description="Ordered remediation steps with explicit risk annotations.",
    )
    rollback_candidate_revision: str | None = Field(
        default=None,
        description="Last-known-good revision ID if a rollback is warranted.",
    )
    requires_human_confirmation: bool = Field(
        default=True,
        description="Must be True whenever any production state mutation is proposed.",
    )


class CriticEvaluationReport(StrictSchema):
    """Self-evaluation output emitted by ``remediation_critic_agent`` inside LoopAgent."""

    verdict: Literal["pass", "revise"] = Field(
        description="'pass' only if grounded, safe, complete, and policy-compliant."
    )
    groundedness_score: float = Field(ge=0.0, le=1.0)
    safety_compliance_score: float = Field(ge=0.0, le=1.0)
    completeness_score: float = Field(ge=0.0, le=1.0)
    policy_violations: list[str] = Field(
        default_factory=list,
        description="Any violated safety or HITL rules that must be corrected.",
    )
    revision_feedback: str = Field(
        description="Concrete instructions for the refiner agent if verdict is 'revise'."
    )


def export_all_tool_json_schemas() -> dict[str, dict[str, Any]]:
    """Export canonical JSON Schema definitions for all tool input/output contracts."""
    models: list[type[BaseModel]] = [
        ServiceMetricsInput,
        ServiceMetricsOutput,
        DeploymentHistoryInput,
        DeploymentHistoryOutput,
        RunbookSearchInput,
        RunbookSearchOutput,
        DependencyTopologyInput,
        DependencyTopologyOutput,
        RollbackDeploymentInput,
        RollbackDeploymentOutput,
        ScaleReplicasInput,
        ScaleReplicasOutput,
        CreateIncidentTicketInput,
        CreateIncidentTicketOutput,
        ToolErrorResponse,
        IncidentTriageReport,
        RemediationPlan,
        CriticEvaluationReport,
    ]
    return {model.__name__: model.model_json_schema() for model in models}
