"""Strategic model routing across Gemini Flash (fast) and Gemini Pro (reasoning).

Routes agent invocations and dynamic LLM requests to the optimal model tier:
- ``ModelTier.FAST`` (``gemini-2.5-flash``): low-latency triage, parallel tool
  diagnostics, runbook search, history compaction summarization, and straightforward
  execution steps.
- ``ModelTier.REASONING`` (``gemini-2.5-pro``): multi-signal root-cause synthesis,
  remediation planning, critical self-evaluation, and dynamic escalation on SEV1
  outages or high-complexity prompts.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from app.config import ModelRoutingConfig

try:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.plugins.base_plugin import BasePlugin
except ImportError:  # pragma: no cover - allows offline unit testing without ADK wheel
    CallbackContext = Any  # type: ignore[assignment,misc]
    LlmRequest = Any  # type: ignore[assignment,misc]
    LlmResponse = Any  # type: ignore[assignment,misc]

    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


class ModelTier(str, Enum):
    """Supported model capability and cost/latency tiers."""

    FAST = "fast"
    REASONING = "reasoning"
    SUMMARIZER = "summarizer"
    JUDGE = "judge"


@dataclass(frozen=True)
class RoutingDecision:
    """Auditable record explaining why a specific model was selected."""

    selected_model: str
    tier: ModelTier
    reason: str
    escalated: bool = False


# Static role-to-tier mapping for multi-agent specialization.
AGENT_DEFAULT_TIERS: dict[str, ModelTier] = {
    "sentinel_sre_coordinator": ModelTier.FAST,
    "incident_triage_agent": ModelTier.FAST,
    "metrics_and_logs_investigator": ModelTier.FAST,
    "deployment_change_investigator": ModelTier.FAST,
    "runbook_rag_investigator": ModelTier.FAST,
    "remediation_executor_agent": ModelTier.FAST,
    "root_cause_planner_agent": ModelTier.REASONING,
    "remediation_critic_agent": ModelTier.REASONING,
    "remediation_refiner_agent": ModelTier.REASONING,
}

_HIGH_COMPLEXITY_SIGNALS = (
    "sev1",
    "p0",
    "outage",
    "cascading failure",
    "root cause",
    "postmortem",
    "rollback",
    "multi-region",
    "data corruption",
    "deadlock",
)


class ModelRouter:
    """Policy engine selecting between Gemini Flash and Gemini Pro."""

    def __init__(self, config: ModelRoutingConfig | None = None) -> None:
        self._config = config or ModelRoutingConfig()

    def model_for_tier(self, tier: ModelTier) -> str:
        """Return the configured Gemini model identifier for a tier."""
        if tier is ModelTier.REASONING:
            return self._config.reasoning_model
        if tier is ModelTier.SUMMARIZER:
            return self._config.summarizer_model
        if tier is ModelTier.JUDGE:
            return self._config.judge_model
        return self._config.fast_model

    def select_for_agent(self, agent_name: str) -> RoutingDecision:
        """Select the baseline model tier for a named specialist agent."""
        tier = AGENT_DEFAULT_TIERS.get(agent_name, ModelTier.FAST)
        model = self.model_for_tier(tier)
        return RoutingDecision(
            selected_model=model,
            tier=tier,
            reason=f"role_policy:{agent_name}->{tier.value}",
            escalated=False,
        )

    def route_request(
        self,
        *,
        agent_name: str,
        prompt_text: str,
        severity: str | None = None,
        requires_deep_reasoning: bool = False,
    ) -> RoutingDecision:
        """Dynamically route a request, escalating from Flash to Pro when justified.

        Escalates a FAST-tier agent (such as the Coordinator) to Gemini Pro when:
        - Severity is SEV1,
        - Triage explicitly set ``requires_deep_reasoning=True``,
        - Prompt length exceeds ``complexity_escalation_chars``, or
        - Prompt contains >= 2 high-complexity incident markers.
        """
        baseline = self.select_for_agent(agent_name)
        if baseline.tier is ModelTier.REASONING:
            return baseline

        # Specialist telemetry fetchers stay on Flash to preserve low-latency fan-out.
        if agent_name in {
            "incident_triage_agent",
            "metrics_and_logs_investigator",
            "deployment_change_investigator",
            "runbook_rag_investigator",
        }:
            return baseline

        normalized = prompt_text.lower()
        matched_signals = [s for s in _HIGH_COMPLEXITY_SIGNALS if s in normalized]

        if severity and severity.upper() == "SEV1":
            return RoutingDecision(
                selected_model=self._config.reasoning_model,
                tier=ModelTier.REASONING,
                reason="dynamic_escalation:sev1_incident",
                escalated=True,
            )
        if requires_deep_reasoning:
            return RoutingDecision(
                selected_model=self._config.reasoning_model,
                tier=ModelTier.REASONING,
                reason="dynamic_escalation:triage_requested_deep_reasoning",
                escalated=True,
            )
        if len(prompt_text) >= self._config.complexity_escalation_chars:
            return RoutingDecision(
                selected_model=self._config.reasoning_model,
                tier=ModelTier.REASONING,
                reason="dynamic_escalation:high_prompt_complexity_length",
                escalated=True,
            )
        if len(matched_signals) >= 2:
            return RoutingDecision(
                selected_model=self._config.reasoning_model,
                tier=ModelTier.REASONING,
                reason=f"dynamic_escalation:signals={','.join(matched_signals[:3])}",
                escalated=True,
            )

        return baseline


class StrategicModelRoutingPlugin(BasePlugin):
    """ADK plugin that dynamically enforces Flash/Pro routing in ``before_model_callback``."""

    def __init__(
        self,
        router: ModelRouter | None = None,
        name: str = "strategic_model_routing_plugin",
    ) -> None:
        super().__init__(name=name)
        self._router = router or ModelRouter()

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        """Inspect request context and assign the optimal Gemini model tier."""
        agent_name = getattr(callback_context, "agent_name", "sentinel_sre_coordinator")
        state = getattr(callback_context, "state", {}) or {}

        triage = state.get("triage_report")
        severity: str | None = None
        requires_deep = False
        if isinstance(triage, dict):
            severity = triage.get("severity")
            requires_deep = bool(triage.get("requires_deep_reasoning", False))

        prompt_parts: list[str] = []
        for content in getattr(llm_request, "contents", None) or []:
            for part in getattr(content, "parts", None) or []:
                text = getattr(part, "text", None)
                if text:
                    prompt_parts.append(text)
        prompt_text = "\n".join(prompt_parts)

        decision = self._router.route_request(
            agent_name=agent_name,
            prompt_text=prompt_text,
            severity=severity,
            requires_deep_reasoning=requires_deep,
        )

        if hasattr(llm_request, "model"):
            llm_request.model = decision.selected_model

        if isinstance(state, dict) or hasattr(state, "__setitem__"):
            state["temp:last_routed_model"] = decision.selected_model
            state["temp:last_routing_reason"] = decision.reason
            state["temp:last_routing_escalated"] = decision.escalated

        return None
