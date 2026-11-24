"""Unit tests for Structured JSON Logging, Intent-vs-Outcome Capture, & OTel Tracing."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

from app.observability.logging import configure_logging, get_logger
from app.observability.observability_plugin import ObservabilityPlugin
from app.observability.tracing import (
    OTEL_AVAILABLE,
    clear_fallback_recorded_spans,
    configure_tracing,
    get_fallback_recorded_spans,
    get_tracer,
)


def test_structured_json_logging_scrubs_pii_and_includes_trace_ids(caplog: Any) -> None:
    configure_logging()
    tracer = get_tracer("test.observability")
    log = get_logger("test.structured")

    with caplog.at_level(logging.INFO):
        with tracer.start_as_current_span("root.sre.request"):
            log.info(
                "incident_received",
                reporter_email="alice@example.com",
                service_name="checkout-api",
            )

    json_records = []
    for record in caplog.records:
        try:
            json_records.append(json.loads(record.getMessage()))
        except json.JSONDecodeError:
            continue

    assert json_records, "Expected structured JSON log entries."
    entry = json_records[-1]
    assert entry["event"] == "incident_received"
    assert entry["severity"] == "INFO"
    assert entry["service_name"] == "checkout-api"
    assert entry["reporter_email"] == "[REDACTED_EMAIL]"
    assert entry.get("trace_id"), "Expected trace_id correlation in structured JSON log."
    assert entry.get("span_id"), "Expected span_id correlation in structured JSON log."


def test_observability_plugin_captures_paired_intent_and_outcome_records() -> None:
    plugin = ObservabilityPlugin()
    tool = SimpleNamespace(name="rollback_production_deployment")
    tool_ctx = SimpleNamespace(
        agent_name="remediation_executor_agent",
        function_call_id="fc-99",
    )

    async def _run_lifecycle() -> dict[str, Any]:
        await plugin.before_tool_callback(
            tool=tool,
            tool_args={
                "service_name": "checkout-api",
                "justification": "Reported by ops@example.com",
            },
            tool_context=tool_ctx,
        )
        await plugin.after_tool_callback(
            tool=tool,
            tool_args={"service_name": "checkout-api"},
            tool_context=tool_ctx,
            result={
                "status": "awaiting_human_confirmation",
                "human_approved": False,
                "message": "Paused for human approval.",
            },
        )
        # Also verify unexpected runtime error produces guided outcome
        return (
            await plugin.on_tool_error_callback(
                tool=tool,
                tool_args={"service_name": "checkout-api"},
                tool_context=tool_ctx,
                error=RuntimeError("transient timeout"),
            )
            or {}
        )

    guided_err = asyncio.run(_run_lifecycle())
    assert guided_err["status"] == "error"
    assert guided_err["recovery_instructions"]

    tool_records = [r for r in plugin.audit_records if r.category == "tool"]
    assert len(tool_records) == 3

    intent_rec, outcome_rec, error_rec = tool_records
    assert intent_rec.phase == "intent"
    assert outcome_rec.phase == "outcome"
    assert intent_rec.action_correlation_id == outcome_rec.action_correlation_id
    assert "ops@example.com" not in json.dumps(intent_rec.payload)
    assert outcome_rec.payload["actual_status"] == "awaiting_human_confirmation"
    assert outcome_rec.intent_fulfilled is False
    assert error_rec.phase == "outcome"
    assert error_rec.payload["actual_status"] == "error"


def test_distributed_tracing_links_parent_and_child_spans() -> None:
    if OTEL_AVAILABLE:
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        exporter = InMemorySpanExporter()
        configure_tracing(extra_exporter=exporter)
        tracer = get_tracer("test.otel")
        with tracer.start_as_current_span("parent.incident.handle") as parent:
            with tracer.start_as_current_span("child.tool.metrics") as child:
                child.set_attribute("sentinel.service", "checkout-api")
        spans = exporter.get_finished_spans()
        names = [s.name for s in spans]
        assert "child.tool.metrics" in names
        assert "parent.incident.handle" in names
        child_span = next(s for s in spans if s.name == "child.tool.metrics")
        parent_span = next(s for s in spans if s.name == "parent.incident.handle")
        assert child_span.context.trace_id == parent_span.context.trace_id
        assert child_span.parent.span_id == parent_span.context.span_id
    else:
        clear_fallback_recorded_spans()
        tracer = get_tracer("test.fallback")
        with tracer.start_as_current_span("parent.incident.handle"):
            with tracer.start_as_current_span("child.tool.metrics"):
                pass
        spans = get_fallback_recorded_spans()
        assert len(spans) == 2
        child_span, parent_span = spans[0], spans[1]
        assert child_span.trace_id == parent_span.trace_id
        assert child_span.parent_span_id == parent_span.span_id
