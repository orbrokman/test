"""ADK Observability Plugin capturing Intent vs. Outcome and OpenTelemetry spans.

For every agent run, LLM invocation, and tool execution, this plugin logs two
explicitly correlated structured JSON audit records linked by
``action_correlation_id`` and an OpenTelemetry span:
1. **Intent** (``phase="intent"``) captured BEFORE execution: records what the
   agent *intended* to do (tool name, PII-scrubbed arguments, risk level,
   routed model).
2. **Outcome** (``phase="outcome"``) captured AFTER execution (or on error):
   records what *actually* happened (status, duration_ms, human_approved,
   ``intent_fulfilled``, error_code, and deviation reason).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any

from app.observability.logging import get_logger
from app.observability.tracing import get_current_trace_context, get_tracer
from app.security.pii_redactor import PiiRedactor, get_pii_redactor
from app.tools.errors import build_guided_error

try:
    from google.adk.agents.base_agent import BaseAgent
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.plugins.base_plugin import BasePlugin
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext
except ImportError:  # pragma: no cover - optional offline import guard
    BaseAgent = Any  # type: ignore[assignment,misc]
    CallbackContext = Any  # type: ignore[assignment,misc]
    LlmRequest = Any  # type: ignore[assignment,misc]
    LlmResponse = Any  # type: ignore[assignment,misc]
    BaseTool = Any  # type: ignore[assignment,misc]
    ToolContext = Any  # type: ignore[assignment,misc]

    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


logger = get_logger(__name__)
tracer = get_tracer(__name__)

_HIGH_RISK_TOOLS = frozenset(
    {
        "rollback_production_deployment",
        "scale_cloud_run_replicas",
        "create_critical_incident_ticket",
    }
)


@dataclass(frozen=True)
class IntentOutcomeRecord:
    """Structured audit entry pairing an intended action with its actual outcome."""

    action_correlation_id: str
    phase: str  # "intent" or "outcome"
    category: str  # "agent" | "model" | "tool"
    agent_name: str
    target_name: str
    payload: dict[str, Any]
    trace_id: str
    span_id: str
    duration_ms: float | None = None
    intent_fulfilled: bool | None = None


class ObservabilityPlugin(BasePlugin):
    """Global ADK plugin for Intent-vs-Outcome structured audit logs & OTel spans."""

    def __init__(
        self,
        *,
        redactor: PiiRedactor | None = None,
        name: str = "sentinel_observability_plugin",
    ) -> None:
        super().__init__(name=name)
        self._redactor = redactor or get_pii_redactor()
        self._in_flight_tools: dict[str, dict[str, Any]] = {}
        self._in_flight_agents: dict[str, dict[str, Any]] = {}
        self._in_flight_models: dict[str, dict[str, Any]] = {}
        self.audit_records: list[IntentOutcomeRecord] = []

    def _record(
        self,
        *,
        correlation_id: str,
        phase: str,
        category: str,
        agent_name: str,
        target_name: str,
        payload: dict[str, Any],
        duration_ms: float | None = None,
        intent_fulfilled: bool | None = None,
    ) -> IntentOutcomeRecord:
        trace_ctx = get_current_trace_context()
        scrubbed_payload = self._redactor.redact_payload(payload)
        entry = IntentOutcomeRecord(
            action_correlation_id=correlation_id,
            phase=phase,
            category=category,
            agent_name=agent_name,
            target_name=target_name,
            payload=scrubbed_payload,
            trace_id=trace_ctx["trace_id"],
            span_id=trace_ctx["span_id"],
            duration_ms=duration_ms,
            intent_fulfilled=intent_fulfilled,
        )
        self.audit_records.append(entry)
        return entry

    def _tool_key(self, tool_name: str, tool_context: Any) -> str:
        call_id = getattr(tool_context, "function_call_id", None) or "default"
        agent_name = getattr(tool_context, "agent_name", "unknown")
        return f"{agent_name}:{tool_name}:{call_id}"

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Any:
        agent_name = getattr(agent, "name", "unknown")
        invocation_id = getattr(callback_context, "invocation_id", "local")
        correlation_id = f"agent-{uuid.uuid4().hex[:12]}"
        key = f"{agent_name}:{invocation_id}"

        span = tracer.start_span(f"agent.run.{agent_name}")
        if hasattr(span, "set_attribute"):
            span.set_attribute("sentinel.agent_name", agent_name)
            span.set_attribute("sentinel.correlation_id", correlation_id)

        self._in_flight_agents[key] = {
            "correlation_id": correlation_id,
            "started": time.perf_counter(),
            "span": span,
        }
        self._record(
            correlation_id=correlation_id,
            phase="intent",
            category="agent",
            agent_name=agent_name,
            target_name=agent_name,
            payload={"invocation_id": invocation_id, "action": "run_agent"},
        )
        logger.info(
            "agent_run_intent",
            phase="intent",
            action_correlation_id=correlation_id,
            agent_name=agent_name,
            invocation_id=invocation_id,
        )
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> Any:
        agent_name = getattr(agent, "name", "unknown")
        invocation_id = getattr(callback_context, "invocation_id", "local")
        key = f"{agent_name}:{invocation_id}"
        ctx = self._in_flight_agents.pop(key, None)

        correlation_id = ctx["correlation_id"] if ctx else f"agent-{uuid.uuid4().hex[:12]}"
        duration_ms = (
            round((time.perf_counter() - ctx["started"]) * 1000.0, 2)
            if ctx
            else 0.0
        )
        span = ctx["span"] if ctx else None
        if span is not None and hasattr(span, "end"):
            span.set_attribute("sentinel.duration_ms", duration_ms)
            span.end()

        self._record(
            correlation_id=correlation_id,
            phase="outcome",
            category="agent",
            agent_name=agent_name,
            target_name=agent_name,
            payload={"invocation_id": invocation_id, "status": "completed"},
            duration_ms=duration_ms,
            intent_fulfilled=True,
        )
        logger.info(
            "agent_run_outcome",
            phase="outcome",
            action_correlation_id=correlation_id,
            agent_name=agent_name,
            invocation_id=invocation_id,
            duration_ms=duration_ms,
            intent_fulfilled=True,
        )
        return None

    async def before_model_callback(
        self, *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> LlmResponse | None:
        agent_name = getattr(callback_context, "agent_name", "unknown")
        model_name = getattr(llm_request, "model", "unknown")
        correlation_id = f"llm-{uuid.uuid4().hex[:12]}"
        self._in_flight_models[agent_name] = {
            "correlation_id": correlation_id,
            "model": model_name,
            "started": time.perf_counter(),
        }
        state = getattr(callback_context, "state", {}) or {}
        routing_reason = (
            state.get("temp:last_routing_reason", "default")
            if isinstance(state, dict) or hasattr(state, "get")
            else "default"
        )
        self._record(
            correlation_id=correlation_id,
            phase="intent",
            category="model",
            agent_name=agent_name,
            target_name=str(model_name),
            payload={"model": str(model_name), "routing_reason": routing_reason},
        )
        logger.info(
            "llm_call_intent",
            phase="intent",
            action_correlation_id=correlation_id,
            agent_name=agent_name,
            model=str(model_name),
            routing_reason=routing_reason,
        )
        return None

    async def after_model_callback(
        self, *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> LlmResponse | None:
        agent_name = getattr(callback_context, "agent_name", "unknown")
        ctx = self._in_flight_models.pop(agent_name, None)
        correlation_id = ctx["correlation_id"] if ctx else f"llm-{uuid.uuid4().hex[:12]}"
        model_name = ctx["model"] if ctx else "unknown"
        duration_ms = (
            round((time.perf_counter() - ctx["started"]) * 1000.0, 2)
            if ctx
            else 0.0
        )
        usage = getattr(llm_response, "usage_metadata", None)
        total_tokens = getattr(usage, "total_token_count", None) if usage else None

        self._record(
            correlation_id=correlation_id,
            phase="outcome",
            category="model",
            agent_name=agent_name,
            target_name=str(model_name),
            payload={
                "model": str(model_name),
                "total_tokens": total_tokens,
                "status": "success",
            },
            duration_ms=duration_ms,
            intent_fulfilled=True,
        )
        logger.info(
            "llm_call_outcome",
            phase="outcome",
            action_correlation_id=correlation_id,
            agent_name=agent_name,
            model=str(model_name),
            total_tokens=total_tokens,
            duration_ms=duration_ms,
            intent_fulfilled=True,
        )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Record explicit action INTENT prior to tool execution."""
        tool_name = getattr(tool, "name", str(tool))
        agent_name = getattr(tool_context, "agent_name", "unknown")
        correlation_id = f"tool-{uuid.uuid4().hex[:12]}"
        risk_tier = "high_stakes_mutating" if tool_name in _HIGH_RISK_TOOLS else "read_only"
        scrubbed_args = self._redactor.redact_payload(tool_args)

        span = tracer.start_span(f"tool.execute.{tool_name}")
        if hasattr(span, "set_attribute"):
            span.set_attribute("sentinel.tool_name", tool_name)
            span.set_attribute("sentinel.agent_name", agent_name)
            span.set_attribute("sentinel.risk_tier", risk_tier)
            span.set_attribute("sentinel.correlation_id", correlation_id)

        key = self._tool_key(tool_name, tool_context)
        self._in_flight_tools[key] = {
            "correlation_id": correlation_id,
            "started": time.perf_counter(),
            "span": span,
            "risk_tier": risk_tier,
        }

        self._record(
            correlation_id=correlation_id,
            phase="intent",
            category="tool",
            agent_name=agent_name,
            target_name=tool_name,
            payload={
                "intended_tool": tool_name,
                "intended_args": scrubbed_args,
                "risk_tier": risk_tier,
            },
        )
        logger.info(
            "tool_action_intent",
            phase="intent",
            action_correlation_id=correlation_id,
            agent_name=agent_name,
            tool_name=tool_name,
            risk_tier=risk_tier,
            intended_args=scrubbed_args,
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Record actual OUTCOME after tool execution and compare against intent."""
        del tool_args
        tool_name = getattr(tool, "name", str(tool))
        agent_name = getattr(tool_context, "agent_name", "unknown")
        key = self._tool_key(tool_name, tool_context)
        ctx = self._in_flight_tools.pop(key, None)

        correlation_id = ctx["correlation_id"] if ctx else f"tool-{uuid.uuid4().hex[:12]}"
        duration_ms = (
            round((time.perf_counter() - ctx["started"]) * 1000.0, 2)
            if ctx
            else 0.0
        )
        actual_status = (
            str(result.get("status", "success"))
            if isinstance(result, dict)
            else "success"
        )
        intent_fulfilled = actual_status == "success"
        human_approved = (
            result.get("human_approved") if isinstance(result, dict) else None
        )
        error_code = result.get("error_code") if isinstance(result, dict) else None
        deviation_reason = (
            None
            if intent_fulfilled
            else (
                result.get("message", f"Tool returned status '{actual_status}'")
                if isinstance(result, dict)
                else f"Unexpected status '{actual_status}'"
            )
        )

        span = ctx["span"] if ctx else None
        if span is not None:
            if hasattr(span, "set_attribute"):
                span.set_attribute("sentinel.actual_status", actual_status)
                span.set_attribute("sentinel.intent_fulfilled", intent_fulfilled)
                span.set_attribute("sentinel.duration_ms", duration_ms)
            if hasattr(span, "end"):
                span.end()

        self._record(
            correlation_id=correlation_id,
            phase="outcome",
            category="tool",
            agent_name=agent_name,
            target_name=tool_name,
            payload={
                "actual_status": actual_status,
                "human_approved": human_approved,
                "error_code": error_code,
                "deviation_reason": deviation_reason,
            },
            duration_ms=duration_ms,
            intent_fulfilled=intent_fulfilled,
        )
        logger.info(
            "tool_action_outcome",
            phase="outcome",
            action_correlation_id=correlation_id,
            agent_name=agent_name,
            tool_name=tool_name,
            actual_status=actual_status,
            intent_fulfilled=intent_fulfilled,
            human_approved=human_approved,
            error_code=error_code,
            deviation_reason=deviation_reason,
            duration_ms=duration_ms,
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        """Capture failed outcome telemetry and return guided LLM recovery payload."""
        del tool_args
        tool_name = getattr(tool, "name", str(tool))
        agent_name = getattr(tool_context, "agent_name", "unknown")
        key = self._tool_key(tool_name, tool_context)
        ctx = self._in_flight_tools.pop(key, None)

        correlation_id = ctx["correlation_id"] if ctx else f"tool-{uuid.uuid4().hex[:12]}"
        duration_ms = (
            round((time.perf_counter() - ctx["started"]) * 1000.0, 2)
            if ctx
            else 0.0
        )
        span = ctx["span"] if ctx else None
        if span is not None:
            if hasattr(span, "record_exception"):
                span.record_exception(error)
            if hasattr(span, "end"):
                span.end()

        self._record(
            correlation_id=correlation_id,
            phase="outcome",
            category="tool",
            agent_name=agent_name,
            target_name=tool_name,
            payload={
                "actual_status": "error",
                "error_type": type(error).__name__,
                "error_message": str(error),
            },
            duration_ms=duration_ms,
            intent_fulfilled=False,
        )
        logger.error(
            "tool_action_outcome",
            phase="outcome",
            action_correlation_id=correlation_id,
            agent_name=agent_name,
            tool_name=tool_name,
            actual_status="error",
            intent_fulfilled=False,
            error_type=type(error).__name__,
            error_message=str(error),
            duration_ms=duration_ms,
        )
        return build_guided_error(
            tool_name=tool_name,
            error_code="UNEXPECTED_TOOL_RUNTIME_ERROR",
            message=f"Tool '{tool_name}' encountered an unexpected {type(error).__name__}.",
            recovery_instructions=[
                "Inspect the parameters you passed and verify all preconditions.",
                "Retry with simpler arguments or escalate the diagnostic failure to the human engineer.",
            ],
            retryable=True,
        )
