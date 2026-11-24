"""Sentinel SRE Multi-Agent ADK Application Topology.

Implements a proven multi-agent orchestration hierarchy in Google ADK:
1. **Coordinator Agent** (``sentinel_sre_coordinator``, Gemini Flash + dynamic
   Pro escalation): Orchestrates the end-to-end SRE incident workflow, preloads
   cross-session memories via ``PreloadMemoryTool``, and enforces HITL stops.
2. **Incident Triage Specialist** (``incident_triage_agent``, Gemini Flash,
   strict ``IncidentTriageReport`` JSON output schema).
3. **Incident Investigation Pipeline** (``SequentialAgent``):
   - Stage 1 — **Parallel Diagnostics Crew** (``ParallelAgent`` running three
     read-only Gemini Flash investigators concurrently: metrics/topology,
     deployment changes, and persistent vector runbook RAG + Vertex AI Search).
   - Stage 2 — **Root-Cause & Remediation Planner** (``root_cause_planner_agent``,
     Gemini Pro, strict ``RemediationPlan`` JSON output schema).
   - Stage 3 — **Self-Evaluation Quality Loop** (``LoopAgent`` pairing Gemini Pro
     ``remediation_critic_agent`` with deterministic ``RemediationQualityEscalationChecker``
     and ``remediation_refiner_agent`` until groundedness & safety gates pass).
4. **HITL-Guarded Remediation Executor** (``remediation_executor_agent``,
   Gemini Flash) invoking high-stakes tools protected by explicit human
   confirmation stops.

Wrapped in an ADK ``App`` with:
- ``EventsCompactionConfig`` (sliding window + token-threshold Flash summarizer)
- ``ContextCacheConfig`` (Gemini prompt context caching)
- ``ResumabilityConfig(is_resumable=True)`` (enables HITL pause & resume)
- Global plugins: ``SecurityGuardrailsPlugin``, ``StrategicModelRoutingPlugin``,
  ``ObservabilityPlugin``, and ``AsyncMemoryConsolidationPlugin``.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from google.adk.agents import (
    Agent,
    BaseAgent,
    LoopAgent,
    ParallelAgent,
    SequentialAgent,
)
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.events import Event, EventActions
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai import types as genai_types

from app.config import AppSettings, get_settings
from app.memory.async_consolidator import (
    AsyncMemoryConsolidationPlugin,
    AsyncMemoryConsolidator,
)
from app.memory.compaction import (
    build_context_cache_config,
    build_events_compaction_config,
)
from app.memory.session_factory import create_memory_service
from app.observability.logging import configure_logging, get_logger
from app.observability.observability_plugin import ObservabilityPlugin
from app.observability.tracing import configure_tracing
from app.prompts.constitution import (
    COORDINATOR_CONSTITUTION,
    DEPLOYMENT_INVESTIGATOR_CONSTITUTION,
    METRICS_INVESTIGATOR_CONSTITUTION,
    REMEDIATION_CRITIC_CONSTITUTION,
    REMEDIATION_EXECUTOR_CONSTITUTION,
    REMEDIATION_REFINER_CONSTITUTION,
    ROOT_CAUSE_PLANNER_CONSTITUTION,
    RUNBOOK_INVESTIGATOR_CONSTITUTION,
    TRIAGE_AGENT_CONSTITUTION,
)
from app.routing.model_router import (
    ModelRouter,
    ModelTier,
    StrategicModelRoutingPlugin,
)
from app.schemas.models import (
    CriticEvaluationReport,
    IncidentTriageReport,
    RemediationPlan,
)
from app.security.guardrails_plugin import SecurityGuardrailsPlugin
from app.tools.diagnostics_tools import (
    fetch_service_error_metrics,
    inspect_service_dependency_topology,
    query_recent_deployment_changes,
    search_incident_runbooks_vector_store,
)
from app.tools.knowledge_store import build_vertex_ai_search_tool
from app.tools.remediation_tools import (
    create_critical_incident_ticket_tool,
    rollback_production_deployment_tool,
    scale_cloud_run_replicas_tool,
)

logger = get_logger(__name__)


def evaluate_critic_gate(critic_result: Any) -> bool:
    """Pure policy function deciding whether ``LoopAgent`` should escalate/exit.

    Requires verdict == 'pass', groundedness >= 0.8, safety_compliance >= 0.9,
    completeness >= 0.8, and zero policy violations.
    """
    if isinstance(critic_result, CriticEvaluationReport):
        data = critic_result.model_dump()
    elif isinstance(critic_result, dict):
        data = critic_result
    else:
        return False

    return (
        data.get("verdict") == "pass"
        and float(data.get("groundedness_score", 0.0)) >= 0.8
        and float(data.get("safety_compliance_score", 0.0)) >= 0.9
        and float(data.get("completeness_score", 0.0)) >= 0.8
        and len(data.get("policy_violations") or []) == 0
    )


class RemediationQualityEscalationChecker(BaseAgent):
    """Custom deterministic ADK ``BaseAgent`` terminating the Critic/Refiner loop."""

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event, None]:
        critic_result = ctx.session.state.get("critic_evaluation")
        passed = evaluate_critic_gate(critic_result)
        logger.info(
            "remediation_self_eval_gate_checked",
            agent_name=self.name,
            passed=passed,
        )
        if passed:
            yield Event(
                author=self.name,
                actions=EventActions(escalate=True),
            )
        else:
            yield Event(author=self.name)


# ---------------------------------------------------------------------------
# Factory Functions (ADK best practice: avoid reusing attached sub-agent nodes)
# ---------------------------------------------------------------------------


def create_incident_triage_agent(router: ModelRouter) -> Agent:
    """Create the fast Gemini Flash incident intake & severity triage specialist."""
    return Agent(
        name="incident_triage_agent",
        model=router.model_for_tier(ModelTier.FAST),
        description=(
            "Rapidly classifies incoming SRE alerts into SEV1-SEV4, normalizes "
            "the affected microservice name, and flags deep-reasoning escalations."
        ),
        instruction=TRIAGE_AGENT_CONSTITUTION,
        output_schema=IncidentTriageReport,
        output_key="triage_report",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=1024,
        ),
    )


def create_parallel_diagnostics_crew(
    router: ModelRouter, settings: AppSettings
) -> ParallelAgent:
    """Create the concurrent ``ParallelAgent`` gathering metrics, deploys, & runbooks."""
    fast_model = router.model_for_tier(ModelTier.FAST)

    metrics_investigator = Agent(
        name="metrics_and_logs_investigator",
        model=fast_model,
        description=(
            "Fetches live SLI/SLO error rates, p99 latency, active replicas, "
            "and dependency blast radius for the impacted microservice."
        ),
        instruction=METRICS_INVESTIGATOR_CONSTITUTION,
        tools=[
            fetch_service_error_metrics,
            inspect_service_dependency_topology,
        ],
        output_key="metrics_findings",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.1,
        ),
    )

    deployment_investigator = Agent(
        name="deployment_change_investigator",
        model=fast_model,
        description=(
            "Queries recent deployment revisions, commit SHAs, and verified "
            "last-known-good versions to detect rollout regressions."
        ),
        instruction=DEPLOYMENT_INVESTIGATOR_CONSTITUTION,
        tools=[query_recent_deployment_changes],
        output_key="deployment_findings",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.1,
        ),
    )

    runbook_tools: list[Any] = [search_incident_runbooks_vector_store]
    vertex_search_tool = build_vertex_ai_search_tool(
        settings.vertex_ai_search_datastore_id
    )
    if vertex_search_tool is not None:
        runbook_tools.append(vertex_search_tool)

    runbook_investigator = Agent(
        name="runbook_rag_investigator",
        model=fast_model,
        description=(
            "Searches the persistent incident runbook vector store (and Vertex AI "
            "Search datastore when configured) for approved mitigation steps."
        ),
        instruction=RUNBOOK_INVESTIGATOR_CONSTITUTION,
        tools=runbook_tools,
        output_key="runbook_findings",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.1,
        ),
    )

    return ParallelAgent(
        name="parallel_diagnostics_crew",
        description=(
            "Executes metrics, deployment-change, and runbook RAG investigators "
            "concurrently to minimize Mean Time To Detect (MTTD)."
        ),
        sub_agents=[
            metrics_investigator,
            deployment_investigator,
            runbook_investigator,
        ],
    )


def create_root_cause_planner_agent(router: ModelRouter) -> Agent:
    """Create the Gemini Pro principal planner synthesizing correlated evidence."""
    return Agent(
        name="root_cause_planner_agent",
        model=router.model_for_tier(ModelTier.REASONING),
        description=(
            "Synthesizes parallel telemetry, release history, and runbooks using "
            "Gemini Pro into a strict, evidence-backed RemediationPlan."
        ),
        instruction=ROOT_CAUSE_PLANNER_CONSTITUTION,
        output_schema=RemediationPlan,
        output_key="remediation_plan",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.2,
            max_output_tokens=2048,
        ),
    )


def create_remediation_quality_loop(router: ModelRouter) -> LoopAgent:
    """Create the Critic -> EscalationChecker -> Refiner self-evaluation ``LoopAgent``."""
    pro_model = router.model_for_tier(ModelTier.REASONING)

    critic_agent = Agent(
        name="remediation_critic_agent",
        model=pro_model,
        description=(
            "Audits the candidate RemediationPlan for groundedness, safety, "
            "HITL compliance, and completeness."
        ),
        instruction=REMEDIATION_CRITIC_CONSTITUTION,
        output_schema=CriticEvaluationReport,
        output_key="critic_evaluation",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=1024,
        ),
    )

    escalation_checker = RemediationQualityEscalationChecker(
        name="remediation_quality_escalation_checker",
        description="Exits the self-evaluation loop once all quality & safety gates pass.",
    )

    refiner_agent = Agent(
        name="remediation_refiner_agent",
        model=pro_model,
        description=(
            "Refines the RemediationPlan to resolve every issue flagged by the "
            "self-evaluation critic."
        ),
        instruction=REMEDIATION_REFINER_CONSTITUTION,
        output_schema=RemediationPlan,
        output_key="remediation_plan",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=2048,
        ),
    )

    return LoopAgent(
        name="remediation_quality_loop",
        description=(
            "Iteratively evaluates and refines the remediation plan until "
            "groundedness and safety policy thresholds are satisfied."
        ),
        sub_agents=[critic_agent, escalation_checker, refiner_agent],
        max_iterations=3,
    )


def create_incident_investigation_pipeline(
    router: ModelRouter, settings: AppSettings
) -> SequentialAgent:
    """Create the deterministic ``SequentialAgent`` investigation & planning pipeline."""
    return SequentialAgent(
        name="incident_investigation_pipeline",
        description=(
            "Runs parallel diagnostics (Flash), root-cause synthesis (Pro), "
            "and autonomous self-evaluation/refinement (Pro) in strict sequence."
        ),
        sub_agents=[
            create_parallel_diagnostics_crew(router, settings),
            create_root_cause_planner_agent(router),
            create_remediation_quality_loop(router),
        ],
    )


def create_remediation_executor_agent(router: ModelRouter) -> Agent:
    """Create the HITL-protected execution specialist for production mitigations."""
    return Agent(
        name="remediation_executor_agent",
        model=router.model_for_tier(ModelTier.FAST),
        description=(
            "Executes approved incident mitigations (deployment rollbacks, "
            "Cloud Run replica scaling, and critical incident tickets) strictly "
            "enforcing Human-in-the-Loop confirmation stops."
        ),
        instruction=REMEDIATION_EXECUTOR_CONSTITUTION,
        tools=[
            rollback_production_deployment_tool,
            scale_cloud_run_replicas_tool,
            create_critical_incident_ticket_tool,
        ],
        output_key="execution_result",
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.0,
        ),
    )


def create_root_agent(settings: AppSettings | None = None) -> Agent:
    """Build the top-level ``sentinel_sre_coordinator`` multi-agent tree."""
    cfg = settings or get_settings()
    router = ModelRouter(cfg.models)

    return Agent(
        name="sentinel_sre_coordinator",
        model=router.model_for_tier(ModelTier.FAST),
        description=(
            "Enterprise SRE Incident Command Coordinator orchestrating triage, "
            "parallel diagnostics, Pro root-cause planning, self-evaluation, "
            "and human-in-the-loop production remediation."
        ),
        instruction=COORDINATOR_CONSTITUTION,
        tools=[PreloadMemoryTool()],
        sub_agents=[
            create_incident_triage_agent(router),
            create_incident_investigation_pipeline(router, cfg),
            create_remediation_executor_agent(router),
        ],
        generate_content_config=genai_types.GenerateContentConfig(
            temperature=0.2,
        ),
    )


def create_app(settings: AppSettings | None = None) -> App:
    """Build the full ADK ``App`` with compaction, caching, resumability, & plugins."""
    cfg = settings or get_settings()
    configure_logging(cfg)
    configure_tracing(cfg)

    router = ModelRouter(cfg.models)
    memory_service = create_memory_service(cfg)
    consolidator = AsyncMemoryConsolidator(memory_service=memory_service)

    plugins = [
        SecurityGuardrailsPlugin(settings=cfg),
        StrategicModelRoutingPlugin(router=router),
        ObservabilityPlugin(),
        AsyncMemoryConsolidationPlugin(consolidator=consolidator),
    ]

    return App(
        name=cfg.app_name,
        root_agent=create_root_agent(cfg),
        plugins=plugins,
        events_compaction_config=build_events_compaction_config(cfg),
        context_cache_config=build_context_cache_config(cfg),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )


# Standard ADK discovery entrypoints (`adk web`, `adk run`, `adk eval`).
app = create_app()
root_agent = app.root_agent
