"""Security layer: Secret Manager, Cloud DLP PII redaction, and ADK guardrails."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "PiiRedactor",
    "PolicyEvaluationResult",
    "SecretAccessError",
    "SecretManagerProvider",
    "SecurityGuardrailsPlugin",
    "get_pii_redactor",
    "get_secret_provider",
    "set_pii_redactor_for_testing",
    "set_secret_provider_for_testing",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "PiiRedactor": ("app.security.pii_redactor", "PiiRedactor"),
    "get_pii_redactor": ("app.security.pii_redactor", "get_pii_redactor"),
    "set_pii_redactor_for_testing": (
        "app.security.pii_redactor",
        "set_pii_redactor_for_testing",
    ),
    "SecretAccessError": ("app.security.secrets", "SecretAccessError"),
    "SecretManagerProvider": ("app.security.secrets", "SecretManagerProvider"),
    "get_secret_provider": ("app.security.secrets", "get_secret_provider"),
    "set_secret_provider_for_testing": (
        "app.security.secrets",
        "set_secret_provider_for_testing",
    ),
    "PolicyEvaluationResult": (
        "app.security.guardrails_plugin",
        "PolicyEvaluationResult",
    ),
    "SecurityGuardrailsPlugin": (
        "app.security.guardrails_plugin",
        "SecurityGuardrailsPlugin",
    ),
}


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module_name, attr_name = _EXPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
