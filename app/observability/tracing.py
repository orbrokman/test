"""OpenTelemetry distributed tracing setup with Cloud Trace and OTLP exporters.

Links spans across the entire agent lifecycle:
``request -> agent.invocation -> agent.run -> guardrail.screen -> llm.call -> tool.execute -> memory.consolidate_async``

Exports W3C TraceContext ``trace_id`` and ``span_id`` for correlation with
structured JSON logs and Google Cloud Logging.
"""

from __future__ import annotations

import contextlib
import contextvars
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterator

from app.config import AppSettings, get_settings

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (
        BatchSpanProcessor,
        SimpleSpanProcessor,
        SpanExporter,
    )

    OTEL_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback when opentelemetry wheel absent
    OTEL_AVAILABLE = False
    otel_trace = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]
    TracerProvider = None  # type: ignore[assignment]
    BatchSpanProcessor = None  # type: ignore[assignment]
    SimpleSpanProcessor = None  # type: ignore[assignment]
    SpanExporter = Any  # type: ignore[assignment,misc]


# Lightweight in-process span stack used only if opentelemetry-sdk is unavailable.
_FALLBACK_SPAN_STACK: contextvars.ContextVar[tuple["_FallbackSpan", ...]] = (
    contextvars.ContextVar("sentinel_fallback_span_stack", default=())
)
_FALLBACK_RECORDED_SPANS: list["_FallbackSpan"] = []


@dataclass
class _FallbackSpan:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    status: str = "OK"
    exception: str | None = None

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def record_exception(self, exc: BaseException) -> None:
        self.status = "ERROR"
        self.exception = f"{type(exc).__name__}: {exc}"


class _FallbackTracer:
    """Minimal OpenTelemetry-compatible tracer for bare-stdlib test environments."""

    def __init__(self, name: str) -> None:
        self._name = name

    @contextlib.contextmanager
    def start_as_current_span(self, name: str) -> Iterator[_FallbackSpan]:
        stack = _FALLBACK_SPAN_STACK.get()
        parent = stack[-1] if stack else None
        trace_id = parent.trace_id if parent else uuid.uuid4().hex
        span = _FallbackSpan(
            name=name,
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
        )
        token = _FALLBACK_SPAN_STACK.set((*stack, span))
        try:
            yield span
        except BaseException as exc:
            span.record_exception(exc)
            raise
        finally:
            _FALLBACK_RECORDED_SPANS.append(span)
            _FALLBACK_SPAN_STACK.reset(token)

    def start_span(self, name: str) -> _FallbackSpan:
        stack = _FALLBACK_SPAN_STACK.get()
        parent = stack[-1] if stack else None
        trace_id = parent.trace_id if parent else uuid.uuid4().hex
        span = _FallbackSpan(
            name=name,
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_span_id=parent.span_id if parent else None,
        )
        _FALLBACK_RECORDED_SPANS.append(span)
        return span


_TRACING_CONFIGURED = False


def configure_tracing(
    settings: AppSettings | None = None,
    *,
    extra_exporter: Any | None = None,
) -> Any:
    """Initialize OpenTelemetry ``TracerProvider`` with GCP Cloud Trace or OTLP.

    Args:
        settings: Optional application settings; loaded from env if omitted.
        extra_exporter: Optional ``SpanExporter`` (e.g., ``InMemorySpanExporter``
            in automated tests) attached via ``SimpleSpanProcessor``.

    Returns:
        The configured ``TracerProvider`` (or ``None`` in fallback mode).
    """
    global _TRACING_CONFIGURED
    cfg = settings or get_settings()

    if not OTEL_AVAILABLE:
        _TRACING_CONFIGURED = True
        return None

    resource = Resource.create(
        {
            "service.name": cfg.observability.service_name,
            "service.version": cfg.observability.service_version,
            "deployment.environment": cfg.environment,
        }
    )
    provider = TracerProvider(resource=resource)

    if extra_exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(extra_exporter))

    if cfg.observability.enable_cloud_trace and cfg.gcp_project_id:
        try:
            from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

            cloud_exporter = CloudTraceSpanExporter(project_id=cfg.gcp_project_id)
            provider.add_span_processor(BatchSpanProcessor(cloud_exporter))
        except ImportError:
            pass

    if cfg.observability.otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )

            otlp_exporter = OTLPSpanExporter(
                endpoint=cfg.observability.otlp_endpoint
            )
            provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        except ImportError:
            pass

    otel_trace.set_tracer_provider(provider)
    _TRACING_CONFIGURED = True
    return provider


def get_tracer(instrumentation_name: str) -> Any:
    """Return an OpenTelemetry tracer (or fallback tracer when SDK absent)."""
    if OTEL_AVAILABLE and otel_trace is not None:
        return otel_trace.get_tracer(instrumentation_name)
    return _FallbackTracer(instrumentation_name)


def get_current_trace_context() -> dict[str, str]:
    """Return active ``trace_id`` and ``span_id`` hex strings for log correlation."""
    if OTEL_AVAILABLE and otel_trace is not None:
        span = otel_trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and getattr(ctx, "is_valid", False):
            return {
                "trace_id": f"{ctx.trace_id:032x}",
                "span_id": f"{ctx.span_id:016x}",
            }

    stack = _FALLBACK_SPAN_STACK.get()
    if stack:
        current = stack[-1]
        return {"trace_id": current.trace_id, "span_id": current.span_id}

    return {"trace_id": "", "span_id": ""}


def get_fallback_recorded_spans() -> list[_FallbackSpan]:
    """Inspect recorded fallback spans in offline tests."""
    return list(_FALLBACK_RECORDED_SPANS)


def clear_fallback_recorded_spans() -> None:
    """Reset recorded fallback spans between unit tests."""
    _FALLBACK_RECORDED_SPANS.clear()
