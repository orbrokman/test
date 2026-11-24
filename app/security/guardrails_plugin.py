"""ADK security guardrails, policy enforcement, and Model Armor integration.

Implements a production ``BasePlugin`` (``SecurityGuardrailsPlugin``) covering:
1. **Input Screening (``before_model_callback``)**: Screens prompts against
   Google Cloud Model Armor (when enabled) and deterministic prompt-injection,
   jailbreak, and credential-exfiltration detectors.
2. **Tool Policy Enforcement (``before_tool_callback``)**: Enforces least-privilege
   role isolation (read-only diagnostic agents cannot invoke production mutating
   tools) and blocks dangerous injection payloads in tool arguments with guided
   recovery feedback.
3. **Output Guardrail (``after_model_callback``)**: Screens responses via Model
   Armor (when enabled) and scrubs any accidental PII/secret leakage via
   ``PiiRedactor``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config import AppSettings, get_settings
from app.observability.logging import get_logger
from app.observability.tracing import get_tracer
from app.security.pii_redactor import PiiRedactor, get_pii_redactor
from app.tools.errors import build_guided_error

try:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.models.llm_response import LlmResponse
    from google.adk.plugins.base_plugin import BasePlugin
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - optional offline import guard
    CallbackContext = Any  # type: ignore[assignment,misc]
    LlmRequest = Any  # type: ignore[assignment,misc]
    LlmResponse = Any  # type: ignore[assignment,misc]
    BaseTool = Any  # type: ignore[assignment,misc]
    ToolContext = Any  # type: ignore[assignment,misc]
    genai_types = None  # type: ignore[assignment]

    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


logger = get_logger(__name__)
tracer = get_tracer(__name__)

_PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"(?i)\b(?:ignore|disregard|forget)\s+(?:all\s+)?(?:previous|prior|above|system)\s+(?:instructions|prompts|rules|constitution)\b"
        ),
    ),
    (
        "hitl_bypass_attempt",
        re.compile(
            r"(?i)\b(?:bypass|skip|disable|override)\s+(?:human|hitl|approval|confirmation|guardrails?|safety)\b"
        ),
    ),
    (
        "secret_exfiltration_attempt",
        re.compile(
            r"(?i)\b(?:reveal|print|dump|exfiltrate|output)\s+(?:your\s+)?(?:system\s+prompt|api[_\s]?keys?|secrets?|credentials?|env(?:ironment)?\s+variables?)\b"
        ),
    ),
    (
        "jailbreak_persona",
        re.compile(
            r"(?i)\b(?:jailbreak|developer\s+mode|dan\s+mode|unrestricted\s+mode)\b"
        ),
    ),
)

_DANGEROUS_ARG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("shell_injection", re.compile(r"(?:;\s*rm\s+-rf|\$\([^)]+\)|`[^`]+`|\|\s*bash)")),
    ("sql_injection", re.compile(r"(?i)(?:;\s*drop\s+table|union\s+select\s+|--\s*$)")),
)

# Read-only specialist agents are strictly forbidden from calling mutating tools.
_READ_ONLY_AGENTS = frozenset(
    {
        "incident_triage_agent",
        "metrics_and_logs_investigator",
        "deployment_change_investigator",
        "runbook_rag_investigator",
        "root_cause_planner_agent",
        "remediation_critic_agent",
        "remediation_refiner_agent",
    }
)

_MUTATING_TOOLS = frozenset(
    {
        "rollback_production_deployment",
        "scale_cloud_run_replicas",
        "create_critical_incident_ticket",
    }
)


@dataclass(frozen=True)
class PolicyEvaluationResult:
    """Result of screening text or tool calls against security policies."""

    allowed: bool
    policy_id: str
    reason: str
    sanitized_text: str | None = None


def _build_blocked_llm_response(message: str) -> Any:
    """Construct an ADK ``LlmResponse`` short-circuiting a blocked model call."""
    if genai_types is not None and LlmResponse is not Any:
        return LlmResponse(
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part.from_text(text=message)],
            )
        )
    return {"blocked": True, "text": message}


class SecurityGuardrailsPlugin(BasePlugin):
    """Defense-in-depth ADK guardrail plugin with Model Armor + local policy checks."""

    def __init__(
        self,
        *,
        settings: AppSettings | None = None,
        redactor: PiiRedactor | None = None,
        model_armor_client: Any | None = None,
        name: str = "security_guardrails_plugin",
    ) -> None:
        super().__init__(name=name)
        self._settings = settings or get_settings()
        self._redactor = redactor or get_pii_redactor()
        self._model_armor_client = model_armor_client

    async def _screen_with_model_armor(
        self, *, text: str, direction: str
    ) -> PolicyEvaluationResult | None:
        """Call Google Cloud Model Armor API when enabled via configuration."""
        sec = self._settings.security
        if not sec.enable_model_armor:
            return None

        template = (
            sec.model_armor_prompt_template
            if direction == "input"
            else sec.model_armor_response_template
        )
        if not template:
            return None

        try:
            client = self._model_armor_client
            if client is None:
                from google.cloud import modelarmor_v1

                client = modelarmor_v1.ModelArmorAsyncClient()
                self._model_armor_client = client

            if direction == "input":
                response = await client.sanitize_user_prompt(
                    request={"name": template, "user_prompt_data": {"text": text}}
                )
            else:
                response = await client.sanitize_model_response(
                    request={
                        "name": template,
                        "model_response_data": {"text": text},
                    }
                )

            sanitization = getattr(response, "sanitization_result", None)
            match_state = str(
                getattr(sanitization, "filter_match_state", "")
            ).upper()
            if "MATCH_FOUND" in match_state:
                return PolicyEvaluationResult(
                    allowed=False,
                    policy_id=f"model_armor_{direction}_filter",
                    reason=(
                        f"Blocked by Google Cloud Model Armor ({direction} template)."
                    ),
                )
            return PolicyEvaluationResult(
                allowed=True,
                policy_id=f"model_armor_{direction}_pass",
                reason="Passed Model Armor screening.",
            )
        except Exception as exc:
            logger.warning(
                "model_armor_screening_fallback",
                direction=direction,
                error_type=type(exc).__name__,
            )
            return None

    def evaluate_prompt_safety(self, prompt_text: str) -> PolicyEvaluationResult:
        """Deterministically screen prompt text for injection or exfiltration attacks."""
        for rule_name, pattern in _PROMPT_INJECTION_PATTERNS:
            if pattern.search(prompt_text):
                return PolicyEvaluationResult(
                    allowed=False,
                    policy_id=f"local_policy:{rule_name}",
                    reason=(
                        "Request blocked by Sentinel Security Policy: detected "
                        f"disallowed pattern ({rule_name})."
                    ),
                )
        return PolicyEvaluationResult(
            allowed=True,
            policy_id="local_policy:pass",
            reason="Prompt passed safety checks.",
        )

    def evaluate_tool_policy(
        self,
        *,
        agent_name: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> PolicyEvaluationResult:
        """Enforce least-privilege tool access and argument safety boundaries."""
        if agent_name in _READ_ONLY_AGENTS and tool_name in _MUTATING_TOOLS:
            return PolicyEvaluationResult(
                allowed=False,
                policy_id="rbac_read_only_violation",
                reason=(
                    f"Agent '{agent_name}' has read-only privileges and cannot "
                    f"invoke mutating tool '{tool_name}'. Delegate execution to "
                    "'remediation_executor_agent'."
                ),
            )

        serialized = str(tool_args)
        for rule_name, pattern in _DANGEROUS_ARG_PATTERNS:
            if pattern.search(serialized):
                return PolicyEvaluationResult(
                    allowed=False,
                    policy_id=f"tool_arg_{rule_name}",
                    reason=(
                        f"Blocked unsafe argument pattern ({rule_name}) in call "
                        f"to '{tool_name}'."
                    ),
                )

        return PolicyEvaluationResult(
            allowed=True,
            policy_id="tool_policy:pass",
            reason="Tool call authorized.",
        )

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        """Screen incoming user prompts before sending them to Gemini."""
        agent_name = getattr(callback_context, "agent_name", "unknown")
        contents = getattr(llm_request, "contents", None) or []
        latest_user_text = ""
        for content in reversed(contents):
            if getattr(content, "role", None) == "user":
                parts = getattr(content, "parts", None) or []
                latest_user_text = " ".join(
                    p.text for p in parts if getattr(p, "text", None)
                )
                break

        if not latest_user_text:
            return None

        with tracer.start_as_current_span("guardrail.screen_input") as span:
            if hasattr(span, "set_attribute"):
                span.set_attribute("sentinel.agent_name", agent_name)

            # 1. Deterministic prompt-injection / policy screen
            local_res = self.evaluate_prompt_safety(latest_user_text)
            if not local_res.allowed:
                logger.warning(
                    "guardrail_input_blocked",
                    agent_name=agent_name,
                    policy_id=local_res.policy_id,
                    reason=local_res.reason,
                )
                return _build_blocked_llm_response(
                    f"[SECURITY GUARDRAIL] {local_res.reason} "
                    "Sentinel enforces strict HITL approval and cannot reveal secrets "
                    "or override safety rules."
                )

            # 2. Google Cloud Model Armor screen (when enabled)
            armor_res = await self._screen_with_model_armor(
                text=latest_user_text, direction="input"
            )
            if armor_res is not None and not armor_res.allowed:
                logger.warning(
                    "guardrail_model_armor_blocked",
                    agent_name=agent_name,
                    policy_id=armor_res.policy_id,
                )
                return _build_blocked_llm_response(
                    f"[MODEL ARMOR GUARDRAIL] {armor_res.reason}"
                )

        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Enforce RBAC and argument safety policies before executing any tool."""
        agent_name = getattr(tool_context, "agent_name", "unknown")
        tool_name = getattr(tool, "name", str(tool))

        with tracer.start_as_current_span("guardrail.screen_tool_call") as span:
            if hasattr(span, "set_attribute"):
                span.set_attribute("sentinel.tool_name", tool_name)
                span.set_attribute("sentinel.agent_name", agent_name)

            decision = self.evaluate_tool_policy(
                agent_name=agent_name,
                tool_name=tool_name,
                tool_args=tool_args,
            )
            if not decision.allowed:
                logger.warning(
                    "guardrail_tool_blocked",
                    agent_name=agent_name,
                    tool_name=tool_name,
                    policy_id=decision.policy_id,
                    reason=decision.reason,
                )
                return build_guided_error(
                    tool_name=tool_name,
                    error_code=decision.policy_id.upper(),
                    message=decision.reason,
                    recovery_instructions=[
                        "Respect least-privilege agent boundaries and sanitize arguments.",
                        "Delegate production mutations to remediation_executor_agent with HITL approval.",
                    ],
                    retryable=False,
                )
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        """Screen model outputs via Model Armor and actively scrub leaked PII."""
        agent_name = getattr(callback_context, "agent_name", "unknown")
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) or []

        for part in parts:
            text = getattr(part, "text", None)
            if not isinstance(text, str) or not text:
                continue

            armor_res = await self._screen_with_model_armor(
                text=text, direction="output"
            )
            if armor_res is not None and not armor_res.allowed:
                logger.warning(
                    "guardrail_output_blocked",
                    agent_name=agent_name,
                    policy_id=armor_res.policy_id,
                )
                return _build_blocked_llm_response(
                    "[MODEL ARMOR GUARDRAIL] Model output was withheld by safety policy."
                )

            scrubbed, was_redacted = self._redactor.redact_text(text)
            if was_redacted:
                part.text = scrubbed
                logger.info(
                    "guardrail_output_pii_redacted",
                    agent_name=agent_name,
                )

        return None
