"""Observability package: structured JSON logging, OTel tracing, and Intent-vs-Outcome plugin."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "IntentOutcomeRecord",
    "OTEL_AVAILABLE",
    "ObservabilityPlugin",
    "clear_fallback_recorded_spans",
    "configure_logging",
    "configure_tracing",
    "get_current_trace_context",
    "get_fallback_recorded_spans",
    "get_logger",
    "get_tracer",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "configure_logging": ("app.observability.logging", "configure_logging"),
    "get_logger": ("app.observability.logging", "get_logger"),
    "IntentOutcomeRecord": (
        "app.observability.observability_plugin",
        "IntentOutcomeRecord",
    ),
    "ObservabilityPlugin": (
        "app.observability.observability_plugin",
        "ObservabilityPlugin",
    ),
    "OTEL_AVAILABLE": ("app.observability.tracing", "OTEL_AVAILABLE"),
    "clear_fallback_recorded_spans": (
        "app.observability.tracing",
        "clear_fallback_recorded_spans",
    ),
    "configure_tracing": ("app.observability.tracing", "configure_tracing"),
    "get_current_trace_context": (
        "app.observability.tracing",
        "get_current_trace_context",
    ),
    "get_fallback_recorded_spans": (
        "app.observability.tracing",
        "get_fallback_recorded_spans",
    ),
    "get_tracer": ("app.observability.tracing", "get_tracer"),
}


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module_name, attr_name = _EXPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
