"""Active PII and secret redaction pipeline for logging, memory, and tool payloads.

Combines two defense-in-depth mechanisms:
1. **Google Cloud Sensitive Data Protection (Cloud DLP)** via
   ``google.cloud.dlp_v2.DlpServiceClient.deidentify_content`` when
   ``ENABLE_CLOUD_DLP=true`` and a GCP project is configured.
2. **Deterministic Local Scrubber** using compiled regex + Luhn credit card
   verification to redact emails, US SSNs, credit cards, phone numbers,
   cloud/API keys, JWTs, bearer tokens, and IPv4 addresses even in offline or
   low-latency local environments.
"""

from __future__ import annotations

import re
from typing import Any

from app.config import AppSettings, get_settings

# Ordered from most specific credentials to general PII patterns.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "[REDACTED_GCP_API_KEY]",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    ),
    (
        "[REDACTED_AWS_KEY]",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ),
    (
        "[REDACTED_GITHUB_TOKEN]",
        re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    ),
    (
        "[REDACTED_API_KEY]",
        re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    ),
    (
        "[REDACTED_JWT]",
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    ),
    (
        "Bearer [REDACTED_TOKEN]",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
    ),
)

_PII_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "[REDACTED_EMAIL]",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "[REDACTED_SSN]",
        re.compile(r"\b(?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"),
    ),
    (
        "[REDACTED_PHONE]",
        re.compile(r"(?<!\w)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    ),
    (
        "[REDACTED_IP]",
        re.compile(
            r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}"
            r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"
        ),
    ),
)

_CREDIT_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")

_SENSITIVE_DICT_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "private_key",
        "ssn",
        "credit_card",
        "card_number",
    }
)

_DEFAULT_DLP_INFO_TYPES = (
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SOCIAL_SECURITY_NUMBER",
    "CREDIT_CARD_NUMBER",
    "IP_ADDRESS",
    "GCP_API_KEY",
    "AUTH_TOKEN",
)


def _passes_luhn(digits: str) -> bool:
    """Validate candidate credit card digits using the Luhn checksum algorithm."""
    if not (13 <= len(digits) <= 19) or not digits.isdigit():
        return False
    total = 0
    reverse_digits = digits[::-1]
    for idx, ch in enumerate(reverse_digits):
        num = int(ch)
        if idx % 2 == 1:
            num *= 2
            if num > 9:
                num -= 9
        total += num
    return total % 10 == 0


def _scrub_credit_cards(text: str) -> tuple[str, bool]:
    changed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal changed
        raw = match.group(0)
        digits = re.sub(r"[ -]", "", raw)
        if _passes_luhn(digits):
            changed = True
            return "[REDACTED_CREDIT_CARD]"
        return raw

    scrubbed = _CREDIT_CARD_CANDIDATE_RE.sub(_replace, text)
    return scrubbed, changed


class PiiRedactor:
    """Active PII and secret redactor with Cloud DLP + deterministic fallback."""

    def __init__(
        self,
        *,
        enable_cloud_dlp: bool | None = None,
        gcp_project_id: str | None = None,
        inspect_template: str | None = None,
        deidentify_template: str | None = None,
        dlp_client: Any | None = None,
    ) -> None:
        settings: AppSettings = get_settings()
        self._enable_cloud_dlp = (
            enable_cloud_dlp
            if enable_cloud_dlp is not None
            else settings.security.enable_cloud_dlp
        )
        self._project_id = (
            gcp_project_id if gcp_project_id is not None else settings.gcp_project_id
        )
        self._inspect_template = (
            inspect_template
            if inspect_template is not None
            else settings.security.dlp_inspect_template
        )
        self._deidentify_template = (
            deidentify_template
            if deidentify_template is not None
            else settings.security.dlp_deidentify_template
        )
        self._dlp_client = dlp_client

    def _get_dlp_client(self) -> Any:
        if self._dlp_client is not None:
            return self._dlp_client
        from google.cloud import dlp_v2

        self._dlp_client = dlp_v2.DlpServiceClient()
        return self._dlp_client

    def _deidentify_with_cloud_dlp(self, text: str) -> str | None:
        """Invoke Google Cloud DLP ``deidentify_content`` when enabled."""
        if not (self._enable_cloud_dlp and self._project_id):
            return None
        try:
            client = self._get_dlp_client()
            parent = f"projects/{self._project_id}/locations/global"
            request: dict[str, Any] = {
                "parent": parent,
                "item": {"value": text},
            }
            if self._inspect_template:
                request["inspect_template_name"] = self._inspect_template
            else:
                request["inspect_config"] = {
                    "info_types": [{"name": t} for t in _DEFAULT_DLP_INFO_TYPES]
                }

            if self._deidentify_template:
                request["deidentify_template_name"] = self._deidentify_template
            else:
                request["deidentify_config"] = {
                    "info_type_transformations": {
                        "transformations": [
                            {
                                "primitive_transformation": {
                                    "replace_with_info_type_config": {}
                                }
                            }
                        ]
                    }
                }

            response = client.deidentify_content(request=request)
            return str(response.item.value)
        except Exception:
            # Always fall back to deterministic local redaction rather than leaking PII.
            return None

    def redact_text(self, text: str) -> tuple[str, bool]:
        """Scrub PII and credentials from a string.

        Returns:
            Tuple of ``(redacted_text, was_modified)``.
        """
        if not text:
            return text, False

        original = text
        current = text

        # Pass 1: Cloud DLP (when configured and reachable)
        dlp_result = self._deidentify_with_cloud_dlp(current)
        if dlp_result is not None:
            current = dlp_result

        # Pass 2: Deterministic credentials & PII patterns (always enforced)
        for replacement, pattern in _CREDENTIAL_PATTERNS:
            current = pattern.sub(replacement, current)

        current, _ = _scrub_credit_cards(current)

        for replacement, pattern in _PII_PATTERNS:
            current = pattern.sub(replacement, current)

        return current, (current != original)

    def redact_payload(self, value: Any) -> Any:
        """Recursively redact PII and secret values inside dicts, lists, and strings."""
        if isinstance(value, str):
            scrubbed, _ = self.redact_text(value)
            return scrubbed
        if isinstance(value, dict):
            result: dict[Any, Any] = {}
            for k, v in value.items():
                if isinstance(k, str) and k.lower() in _SENSITIVE_DICT_KEYS:
                    result[k] = "[REDACTED_SECRET_FIELD]"
                else:
                    result[k] = self.redact_payload(v)
            return result
        if isinstance(value, list):
            return [self.redact_payload(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_payload(item) for item in value)
        return value


_DEFAULT_REDACTOR: PiiRedactor | None = None


def get_pii_redactor() -> PiiRedactor:
    """Return the singleton ``PiiRedactor`` instance."""
    global _DEFAULT_REDACTOR
    if _DEFAULT_REDACTOR is None:
        _DEFAULT_REDACTOR = PiiRedactor()
    return _DEFAULT_REDACTOR


def set_pii_redactor_for_testing(redactor: PiiRedactor | None) -> None:
    """Override the singleton redactor during unit/integration tests."""
    global _DEFAULT_REDACTOR
    _DEFAULT_REDACTOR = redactor
