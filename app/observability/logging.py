"""Structured JSON logging with Cloud Logging severity, trace correlation, & PII scrubbing.

All production modules log exclusively through ``get_logger(__name__)``.
Raw ``print()`` calls are forbidden. Every log record is automatically:
1. Scrubbed by ``PiiRedactor`` so sensitive user data and tokens never reach logs.
2. Enriched with active OpenTelemetry ``trace_id``, ``span_id``, and
   ``logging.googleapis.com/trace`` fields for Cloud Trace <-> Cloud Logging linking.
3. Emitted as single-line structured JSON with Cloud Logging ``severity``.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import AppSettings, get_settings
from app.observability.tracing import get_current_trace_context
from app.security.pii_redactor import get_pii_redactor

try:
    import structlog

    STRUCTLOG_AVAILABLE = True
except ImportError:  # pragma: no cover - stdlib structured JSON fallback
    STRUCTLOG_AVAILABLE = False
    structlog = None  # type: ignore[assignment]


_LEVEL_TO_SEVERITY = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "warn": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}


def _enrich_and_scrub_event(
    event_dict: dict[str, Any],
    *,
    service_name: str,
    gcp_project_id: str,
) -> dict[str, Any]:
    """Attach trace context + Cloud Logging fields and scrub PII recursively."""
    redactor = get_pii_redactor()
    scrubbed: dict[str, Any] = redactor.redact_payload(event_dict)

    scrubbed.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    scrubbed.setdefault("service", service_name)

    level = str(scrubbed.get("level", "info")).lower()
    scrubbed["severity"] = _LEVEL_TO_SEVERITY.get(level, "INFO")

    trace_ctx = get_current_trace_context()
    if trace_ctx["trace_id"]:
        scrubbed["trace_id"] = trace_ctx["trace_id"]
        scrubbed["span_id"] = trace_ctx["span_id"]
        if gcp_project_id:
            scrubbed["logging.googleapis.com/trace"] = (
                f"projects/{gcp_project_id}/traces/{trace_ctx['trace_id']}"
            )
            scrubbed["logging.googleapis.com/spanId"] = trace_ctx["span_id"]

    return scrubbed


class _StdlibJsonLogger:
    """Structured JSON logger matching ``structlog`` keyword-argument API."""

    def __init__(
        self,
        name: str,
        *,
        service_name: str,
        gcp_project_id: str,
    ) -> None:
        self._logger = logging.getLogger(name)
        self._name = name
        self._service_name = service_name
        self._gcp_project_id = gcp_project_id

    def _emit(self, level_name: str, event: str, **kwargs: Any) -> None:
        record_dict: dict[str, Any] = {
            "event": event,
            "logger": self._name,
            "level": level_name,
            **kwargs,
        }
        enriched = _enrich_and_scrub_event(
            record_dict,
            service_name=self._service_name,
            gcp_project_id=self._gcp_project_id,
        )
        line = json.dumps(enriched, default=str, sort_keys=True)
        py_level = getattr(logging, level_name.upper(), logging.INFO)
        self._logger.log(py_level, line)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._emit("debug", event, **kwargs)

    def info(self, event: str, **kwargs: Any) -> None:
        self._emit("info", event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._emit("warning", event, **kwargs)

    def error(self, event: str, **kwargs: Any) -> None:
        self._emit("error", event, **kwargs)

    def critical(self, event: str, **kwargs: Any) -> None:
        self._emit("critical", event, **kwargs)


_LOGGING_CONFIGURED = False


def configure_logging(settings: AppSettings | None = None) -> None:
    """Configure global structured JSON logging with PII scrubbing & trace links."""
    global _LOGGING_CONFIGURED
    cfg = settings or get_settings()
    level = getattr(logging, cfg.observability.log_level, logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root_logger.addHandler(handler)

    if STRUCTLOG_AVAILABLE and structlog is not None:

        def _sentinel_processor(
            _logger: Any, _method_name: str, event_dict: dict[str, Any]
        ) -> dict[str, Any]:
            return _enrich_and_scrub_event(
                event_dict,
                service_name=cfg.observability.service_name,
                gcp_project_id=cfg.gcp_project_id,
            )

        structlog.configure(
            processors=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                _sentinel_processor,
                structlog.processors.JSONRenderer(sort_keys=True),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(level),
            logger_factory=structlog.stdlib.LoggerFactory(),
            cache_logger_on_first_use=False,
        )

    _LOGGING_CONFIGURED = True


def get_logger(name: str) -> Any:
    """Return a structured JSON logger with automatic PII scrubbing and trace IDs."""
    cfg = get_settings()
    if not _LOGGING_CONFIGURED:
        configure_logging(cfg)

    if STRUCTLOG_AVAILABLE and structlog is not None:
        return structlog.get_logger(name)

    return _StdlibJsonLogger(
        name,
        service_name=cfg.observability.service_name,
        gcp_project_id=cfg.gcp_project_id,
    )
