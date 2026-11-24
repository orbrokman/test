"""Guided error-handling utilities for Sentinel SRE tools.

Tools NEVER crash with raw tracebacks on invalid LLM arguments or missing
domain entities. Instead, they construct a schema-validated ``ToolErrorResponse``
containing clear, actionable recovery instructions so the LLM can self-correct.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.schemas.models import ToolErrorResponse


def build_guided_error(
    *,
    tool_name: str,
    error_code: str,
    message: str,
    recovery_instructions: list[str],
    suggested_next_tool: str | None = None,
    valid_examples: list[str] | None = None,
    retryable: bool = True,
) -> dict[str, Any]:
    """Build a JSON-serializable guided error payload for the LLM."""
    payload = ToolErrorResponse(
        tool_name=tool_name,
        error_code=error_code,
        message=message,
        recovery_instructions=recovery_instructions,
        suggested_next_tool=suggested_next_tool,
        valid_examples=valid_examples or [],
        retryable=retryable,
    )
    return payload.model_dump()


def build_validation_error(
    *,
    tool_name: str,
    exc: ValidationError,
    valid_examples: list[str] | None = None,
    suggested_next_tool: str | None = None,
) -> dict[str, Any]:
    """Convert a Pydantic ``ValidationError`` into actionable LLM recovery guidance."""
    field_issues: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = err.get("msg", "invalid value")
        field_issues.append(f"Parameter '{loc}': {msg}")

    instructions = [
        f"Fix invalid input arguments: {'; '.join(field_issues)}.",
        "Check the tool docstring and JSON schema constraints (allowed enums, regex format, numeric bounds).",
        "Retry the tool call with corrected parameters.",
    ]
    return build_guided_error(
        tool_name=tool_name,
        error_code="SCHEMA_VALIDATION_ERROR",
        message=f"Input validation failed for {tool_name}: {'; '.join(field_issues)}",
        recovery_instructions=instructions,
        suggested_next_tool=suggested_next_tool,
        valid_examples=valid_examples,
        retryable=True,
    )
