"""Read-only SRE diagnostic tools with strict schemas and guided error handling.

Every function in this module adheres to AgentOps Tool & Interface Design standards:
- Highly descriptive, unambiguous verb-noun naming
- Comprehensive Google-style docstrings describing purpose, arguments, and outputs
- Strict Pydantic v2 input and output JSON schema validation
- Guided recovery instructions returned to the LLM on any error instead of crashing
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.config import get_settings
from app.schemas.models import (
    DependencyTopologyInput,
    DependencyTopologyOutput,
    DeploymentHistoryInput,
    DeploymentHistoryOutput,
    DeploymentRecord,
    RunbookSearchInput,
    RunbookSearchOutput,
    ServiceMetricsInput,
    ServiceMetricsOutput,
)
from app.tools.errors import build_guided_error, build_validation_error
from app.tools.knowledge_store import PersistentRunbookVectorStore

KNOWN_SERVICES: tuple[str, ...] = (
    "checkout-api",
    "payments-gateway",
    "inventory-ledger",
    "auth-token-service",
    "search-indexer",
)

# Deterministic telemetry fixtures representing live Cloud Monitoring / Cloud Deploy state.
_SERVICE_TELEMETRY: dict[str, dict[str, Any]] = {
    "checkout-api": {
        "error_rate_percent": 14.8,
        "p99_latency_ms": 2840.0,
        "requests_per_second": 620.0,
        "active_replicas": 8,
        "top_error_signatures": [
            "HTTP 503 Service Unavailable: Cloud SQL connection pool exhausted (max_pool=20)",
            "psycopg2.OperationalError: timeout expired acquiring db connection",
        ],
        "slo_breached": True,
    },
    "payments-gateway": {
        "error_rate_percent": 6.4,
        "p99_latency_ms": 2310.0,
        "requests_per_second": 410.0,
        "active_replicas": 6,
        "top_error_signatures": [
            "gRPC DEADLINE_EXCEEDED: upstream card-network webhook timeout > 2000ms",
            "CircuitBreakerOpenException: payments-processor-east",
        ],
        "slo_breached": True,
    },
    "inventory-ledger": {
        "error_rate_percent": 3.2,
        "p99_latency_ms": 1420.0,
        "requests_per_second": 980.0,
        "active_replicas": 12,
        "top_error_signatures": [
            "Spanner ABORTED: lock contention on SKU_HOT_PARTITION_04",
        ],
        "slo_breached": True,
    },
    "auth-token-service": {
        "error_rate_percent": 9.1,
        "p99_latency_ms": 480.0,
        "requests_per_second": 1850.0,
        "active_replicas": 10,
        "top_error_signatures": [
            "HTTP 401 UNAUTHENTICATED: JWKS key id 'kid-2026-11' verification failed",
        ],
        "slo_breached": True,
    },
    "search-indexer": {
        "error_rate_percent": 1.4,
        "p99_latency_ms": 890.0,
        "requests_per_second": 240.0,
        "active_replicas": 4,
        "top_error_signatures": [
            "Container terminated with OOMKilled (exit code 137) during batch_size=512",
        ],
        "slo_breached": False,
    },
}

_DEPLOYMENT_HISTORY: dict[str, dict[str, Any]] = {
    "checkout-api": {
        "current_revision_id": "rev-2026-11-24-089",
        "last_known_good_revision_id": "rev-2026-11-20-084",
        "revisions": [
            {
                "revision_id": "rev-2026-11-24-089",
                "deployed_at_utc": "2026-11-24T09:42:00Z",
                "commit_sha": "a91f4c2",
                "deployed_by_role": "ci-cd-release-bot",
                "change_summary": "Reduce Cloud SQL pool size from 100 to 20 and lower timeout to 2s",
                "health_status": "degraded",
                "is_current": True,
            },
            {
                "revision_id": "rev-2026-11-20-084",
                "deployed_at_utc": "2026-11-20T16:10:00Z",
                "commit_sha": "7c02b18",
                "deployed_by_role": "ci-cd-release-bot",
                "change_summary": "Add idempotency key header validation on cart checkout",
                "health_status": "healthy",
                "is_current": False,
            },
        ],
    },
    "payments-gateway": {
        "current_revision_id": "rev-2026-11-23-051",
        "last_known_good_revision_id": "rev-2026-11-18-049",
        "revisions": [
            {
                "revision_id": "rev-2026-11-23-051",
                "deployed_at_utc": "2026-11-23T22:15:00Z",
                "commit_sha": "d44e901",
                "deployed_by_role": "ci-cd-release-bot",
                "change_summary": "Enable aggressive webhook retries without jitter",
                "health_status": "degraded",
                "is_current": True,
            },
            {
                "revision_id": "rev-2026-11-18-049",
                "deployed_at_utc": "2026-11-18T14:00:00Z",
                "commit_sha": "10ab3de",
                "deployed_by_role": "ci-cd-release-bot",
                "change_summary": "Tune gRPC deadline propagation to 3000ms",
                "health_status": "healthy",
                "is_current": False,
            },
        ],
    },
    "inventory-ledger": {
        "current_revision_id": "rev-2026-11-21-033",
        "last_known_good_revision_id": "rev-2026-11-15-031",
        "revisions": [
            {
                "revision_id": "rev-2026-11-21-033",
                "deployed_at_utc": "2026-11-21T11:30:00Z",
                "commit_sha": "f31c8b0",
                "deployed_by_role": "ci-cd-release-bot",
                "change_summary": "Add synchronous reservation lock across flash-sale SKUs",
                "health_status": "degraded",
                "is_current": True,
            },
            {
                "revision_id": "rev-2026-11-15-031",
                "deployed_at_utc": "2026-11-15T08:45:00Z",
                "commit_sha": "519c2e4",
                "deployed_by_role": "ci-cd-release-bot",
                "change_summary": "Shard SKU counters with optimistic concurrency control",
                "health_status": "healthy",
                "is_current": False,
            },
        ],
    },
    "auth-token-service": {
        "current_revision_id": "rev-2026-11-24-112",
        "last_known_good_revision_id": "rev-2026-11-19-109",
        "revisions": [
            {
                "revision_id": "rev-2026-11-24-112",
                "deployed_at_utc": "2026-11-24T08:05:00Z",
                "commit_sha": "b872a11",
                "deployed_by_role": "security-release-bot",
                "change_summary": "Rotate JWKS signing key without overlapping grace window",
                "health_status": "degraded",
                "is_current": True,
            },
            {
                "revision_id": "rev-2026-11-19-109",
                "deployed_at_utc": "2026-11-19T12:00:00Z",
                "commit_sha": "3aa81c9",
                "deployed_by_role": "security-release-bot",
                "change_summary": "Maintain dual-key JWKS cache with 24h overlap",
                "health_status": "healthy",
                "is_current": False,
            },
        ],
    },
    "search-indexer": {
        "current_revision_id": "rev-2026-11-22-019",
        "last_known_good_revision_id": "rev-2026-11-17-017",
        "revisions": [
            {
                "revision_id": "rev-2026-11-22-019",
                "deployed_at_utc": "2026-11-22T19:20:00Z",
                "commit_sha": "9e44d21",
                "deployed_by_role": "ml-ops-bot",
                "change_summary": "Increase embedding batch_size from 128 to 512",
                "health_status": "degraded",
                "is_current": True,
            },
            {
                "revision_id": "rev-2026-11-17-017",
                "deployed_at_utc": "2026-11-17T10:15:00Z",
                "commit_sha": "4f801a2",
                "deployed_by_role": "ml-ops-bot",
                "change_summary": "Stable batch_size=128 with bounded worker memory",
                "health_status": "healthy",
                "is_current": False,
            },
        ],
    },
}

_SERVICE_TOPOLOGY: dict[str, dict[str, Any]] = {
    "checkout-api": {
        "tier": "tier-0",
        "upstream_callers": ["web-storefront", "mobile-bff"],
        "downstream_dependencies": [
            "payments-gateway",
            "inventory-ledger",
            "cloud-sql-orders-primary",
        ],
        "blast_radius_summary": (
            "Direct revenue impact: all customer checkout transactions fail when "
            "checkout-api breaches 5% 5xx SLO."
        ),
        "oncall_team": "sre-commerce-core",
    },
    "payments-gateway": {
        "tier": "tier-0",
        "upstream_callers": ["checkout-api", "subscription-billing"],
        "downstream_dependencies": ["card-network-proxy", "fraud-scoring-api"],
        "blast_radius_summary": (
            "Blocks order finalization and recurring subscription renewals."
        ),
        "oncall_team": "sre-payments-platform",
    },
    "inventory-ledger": {
        "tier": "tier-1",
        "upstream_callers": ["checkout-api", "warehouse-sync"],
        "downstream_dependencies": ["spanner-inventory-global"],
        "blast_radius_summary": (
            "Delays stock reservation; fallback cache can serve read-only catalog."
        ),
        "oncall_team": "sre-supply-chain",
    },
    "auth-token-service": {
        "tier": "tier-0",
        "upstream_callers": [
            "api-gateway",
            "checkout-api",
            "payments-gateway",
            "mobile-bff",
        ],
        "downstream_dependencies": ["secret-manager-jwks", "redis-session-cache"],
        "blast_radius_summary": (
            "Platform-wide authentication failure affecting every authenticated API."
        ),
        "oncall_team": "sre-identity-security",
    },
    "search-indexer": {
        "tier": "tier-2",
        "upstream_callers": ["catalog-pubsub-topic"],
        "downstream_dependencies": ["vertex-ai-search-catalog", "gcs-index-snapshots"],
        "blast_radius_summary": (
            "Catalog freshness lags behind new product updates; existing queries remain served."
        ),
        "oncall_team": "sre-search-discovery",
    },
}

_RUNBOOK_STORE: PersistentRunbookVectorStore | None = None


def _get_runbook_store() -> PersistentRunbookVectorStore:
    global _RUNBOOK_STORE
    if _RUNBOOK_STORE is None:
        settings = get_settings()
        _RUNBOOK_STORE = PersistentRunbookVectorStore(
            db_path=settings.runbook_db_path
        )
    return _RUNBOOK_STORE


def set_runbook_store_for_testing(store: PersistentRunbookVectorStore | None) -> None:
    """Override the singleton runbook store (used in unit/integration tests)."""
    global _RUNBOOK_STORE
    _RUNBOOK_STORE = store


def fetch_service_error_metrics(
    service_name: str,
    environment: str = "production",
    window_minutes: int = 30,
) -> dict[str, Any]:
    """Retrieve live SLI/SLO error rate, p99 latency, replica count, and top errors.

    Use this read-only diagnostic tool during incident triage and investigation
    to obtain quantitative evidence before proposing any remediation.

    Args:
        service_name: Canonical kebab-case microservice identifier. Must be one
            of: 'checkout-api', 'payments-gateway', 'inventory-ledger',
            'auth-token-service', or 'search-indexer'.
        environment: Target deployment environment. Allowed values are
            'production', 'staging', or 'canary'. Defaults to 'production'.
        window_minutes: Telemetry lookback window in minutes, constrained to
            the integer range [5, 1440]. Defaults to 30.

    Returns:
        A JSON-serializable dictionary conforming to ``ServiceMetricsOutput`` on
        success (containing ``error_rate_percent``, ``p99_latency_ms``,
        ``requests_per_second``, ``active_replicas``, ``top_error_signatures``,
        and ``slo_breached``), or ``ToolErrorResponse`` with actionable
        ``recovery_instructions`` if the service name or parameters are invalid.
    """
    tool_name = "fetch_service_error_metrics"
    try:
        validated = ServiceMetricsInput(
            service_name=service_name,
            environment=environment,  # type: ignore[arg-type]
            window_minutes=window_minutes,
        )
    except ValidationError as exc:
        return build_validation_error(
            tool_name=tool_name,
            exc=exc,
            valid_examples=list(KNOWN_SERVICES),
        )

    telemetry = _SERVICE_TELEMETRY.get(validated.service_name)
    if telemetry is None:
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNKNOWN_SERVICE_NAME",
            message=(
                f"Service '{validated.service_name}' was not found in the "
                "production service catalog."
            ),
            recovery_instructions=[
                "Normalize the service name to one of the valid kebab-case identifiers in valid_examples.",
                "If unsure which service owns the alert, call search_incident_runbooks_vector_store with the error signature.",
            ],
            suggested_next_tool="search_incident_runbooks_vector_store",
            valid_examples=list(KNOWN_SERVICES),
            retryable=True,
        )

    env_multiplier = 1.0 if validated.environment == "production" else 0.35
    output = ServiceMetricsOutput(
        service_name=validated.service_name,
        environment=validated.environment,
        window_minutes=validated.window_minutes,
        error_rate_percent=round(
            min(100.0, telemetry["error_rate_percent"] * env_multiplier), 2
        ),
        p99_latency_ms=round(telemetry["p99_latency_ms"] * env_multiplier, 1),
        requests_per_second=round(
            telemetry["requests_per_second"] * env_multiplier, 1
        ),
        active_replicas=telemetry["active_replicas"],
        top_error_signatures=list(telemetry["top_error_signatures"]),
        slo_breached=bool(
            telemetry["slo_breached"] and validated.environment == "production"
        ),
    )
    return output.model_dump()


def query_recent_deployment_changes(
    service_name: str,
    environment: str = "production",
    limit: int = 5,
) -> dict[str, Any]:
    """Query recent release revisions and last-known-good version for a service.

    Use this read-only diagnostic tool to determine whether an incident
    correlates with a recent rollout and to discover the exact verified
    ``last_known_good_revision_id`` required before requesting a rollback.

    Args:
        service_name: Canonical kebab-case microservice identifier (e.g.,
            'checkout-api', 'payments-gateway', 'auth-token-service').
        environment: Deployment environment ('production', 'staging', 'canary').
            Defaults to 'production'.
        limit: Maximum number of recent revision records to return (1 to 20).
            Defaults to 5.

    Returns:
        A JSON-serializable dictionary conforming to ``DeploymentHistoryOutput``
        containing ``current_revision_id``, ``last_known_good_revision_id``, and
        ordered ``revisions``, or ``ToolErrorResponse`` with guided recovery
        steps when inputs are invalid or the service is unknown.
    """
    tool_name = "query_recent_deployment_changes"
    try:
        validated = DeploymentHistoryInput(
            service_name=service_name,
            environment=environment,  # type: ignore[arg-type]
            limit=limit,
        )
    except ValidationError as exc:
        return build_validation_error(
            tool_name=tool_name,
            exc=exc,
            valid_examples=list(KNOWN_SERVICES),
        )

    history = _DEPLOYMENT_HISTORY.get(validated.service_name)
    if history is None:
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNKNOWN_SERVICE_NAME",
            message=(
                f"No deployment pipeline found for service '{validated.service_name}'."
            ),
            recovery_instructions=[
                "Select a valid service name from valid_examples and retry.",
                "Use inspect_service_dependency_topology if investigating a related component.",
            ],
            suggested_next_tool="inspect_service_dependency_topology",
            valid_examples=list(KNOWN_SERVICES),
            retryable=True,
        )

    revisions = [
        DeploymentRecord(**rev) for rev in history["revisions"][: validated.limit]
    ]
    output = DeploymentHistoryOutput(
        service_name=validated.service_name,
        environment=validated.environment,
        current_revision_id=history["current_revision_id"],
        last_known_good_revision_id=history["last_known_good_revision_id"],
        revisions=revisions,
    )
    return output.model_dump()


def search_incident_runbooks_vector_store(
    query: str,
    service_name: str = "",
    top_k: int = 3,
) -> dict[str, Any]:
    """Search the persistent incident runbook vector store for mitigation procedures.

    Performs cosine-similarity vector search over persisted SRE runbooks and
    historical postmortems to ground root-cause analysis and remediation steps
    in approved procedures.

    Args:
        query: Natural-language description of the incident symptoms, HTTP/gRPC
            error signatures, or failure mode (3 to 500 characters).
        service_name: Optional kebab-case service name to filter and boost
            matches (leave empty string '' to search across all services).
        top_k: Number of top matching runbooks to return (integer 1 to 10).
            Defaults to 3.

    Returns:
        A JSON-serializable dictionary conforming to ``RunbookSearchOutput``
        with ranked ``matches`` (each including ``runbook_id``,
        ``similarity_score``, ``root_cause_pattern``, ``mitigation_steps``, and
        ``requires_human_approval``), or ``ToolErrorResponse`` on validation error.
    """
    tool_name = "search_incident_runbooks_vector_store"
    normalized_service = service_name.strip() or None
    try:
        validated = RunbookSearchInput(
            query=query,
            service_name=normalized_service,
            top_k=top_k,
        )
    except ValidationError as exc:
        return build_validation_error(
            tool_name=tool_name,
            exc=exc,
            valid_examples=[
                "query='HTTP 503 Cloud SQL connection pool exhausted', service_name='checkout-api'"
            ],
        )

    store = _get_runbook_store()
    matches = store.search(
        query=validated.query,
        service_name=validated.service_name,
        top_k=validated.top_k,
    )
    output = RunbookSearchOutput(
        query=validated.query,
        store_backend="sqlite_persistent_vector_store",
        matches=matches,
    )
    return output.model_dump()


def inspect_service_dependency_topology(service_name: str) -> dict[str, Any]:
    """Inspect upstream callers, downstream dependencies, tier, and blast radius.

    Use this read-only tool to assess customer impact, identify owning on-call
    teams, and verify cascading failure paths before proposing a rollback or
    replica scaling operation.

    Args:
        service_name: Canonical kebab-case microservice identifier (e.g.,
            'checkout-api', 'payments-gateway', 'auth-token-service').

    Returns:
        A JSON-serializable dictionary conforming to ``DependencyTopologyOutput``
        with ``tier``, ``upstream_callers``, ``downstream_dependencies``,
        ``blast_radius_summary``, and ``oncall_team``, or ``ToolErrorResponse``
        with guided recovery instructions if the service is unrecognized.
    """
    tool_name = "inspect_service_dependency_topology"
    try:
        validated = DependencyTopologyInput(service_name=service_name)
    except ValidationError as exc:
        return build_validation_error(
            tool_name=tool_name,
            exc=exc,
            valid_examples=list(KNOWN_SERVICES),
        )

    topo = _SERVICE_TOPOLOGY.get(validated.service_name)
    if topo is None:
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNKNOWN_SERVICE_NAME",
            message=(
                f"Service '{validated.service_name}' does not exist in the "
                "dependency topology graph."
            ),
            recovery_instructions=[
                "Retry with one of the registered service names listed in valid_examples.",
                "Do not invent service identifiers that are absent from the service catalog.",
            ],
            suggested_next_tool="fetch_service_error_metrics",
            valid_examples=list(KNOWN_SERVICES),
            retryable=True,
        )

    output = DependencyTopologyOutput(
        service_name=validated.service_name,
        tier=topo["tier"],
        upstream_callers=list(topo["upstream_callers"]),
        downstream_dependencies=list(topo["downstream_dependencies"]),
        blast_radius_summary=topo["blast_radius_summary"],
        oncall_team=topo["oncall_team"],
    )
    return output.model_dump()
