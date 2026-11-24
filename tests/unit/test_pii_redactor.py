"""Unit tests verifying active PII & credential redaction (Cloud DLP + local fallback)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.security.pii_redactor import PiiRedactor


def test_local_redactor_scrubs_all_pii_and_credential_classes() -> None:
    redactor = PiiRedactor(enable_cloud_dlp=False, gcp_project_id="")
    raw = (
        "Alert from alice.doe@corp-example.com (phone 415-555-0199, SSN 123-45-6789) "
        "on host 10.24.8.15 with card 4532-0151-1283-0366 and key "
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456ghi789"
    )
    scrubbed, modified = redactor.redact_text(raw)
    assert modified is True
    assert "alice.doe@corp-example.com" not in scrubbed
    assert "415-555-0199" not in scrubbed
    assert "123-45-6789" not in scrubbed
    assert "4532-0151-1283-0366" not in scrubbed
    assert "10.24.8.15" not in scrubbed
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_SSN]" in scrubbed
    assert "[REDACTED_CREDIT_CARD]" in scrubbed
    assert "[REDACTED_IP]" in scrubbed


def test_recursive_payload_redaction_scrubs_sensitive_keys() -> None:
    redactor = PiiRedactor(enable_cloud_dlp=False, gcp_project_id="")
    payload = {
        "user": "bob@example.com",
        "api_key": "super-secret-value",
        "nested": [{"authorization": "Bearer abcdef1234567890xyz", "ok": "checkout-api"}],
    }
    clean = redactor.redact_payload(payload)
    assert clean["user"] == "[REDACTED_EMAIL]"
    assert clean["api_key"] == "[REDACTED_SECRET_FIELD]"
    assert clean["nested"][0]["authorization"] == "[REDACTED_SECRET_FIELD]"
    assert clean["nested"][0]["ok"] == "checkout-api"


def test_cloud_dlp_integration_invoked_when_enabled() -> None:
    calls: list[dict[str, Any]] = []

    class _MockDlpClient:
        def deidentify_content(self, *, request: dict[str, Any]) -> Any:
            calls.append(request)
            val = request["item"]["value"].replace(
                "alice@example.com", "[EMAIL_ADDRESS]"
            )
            return SimpleNamespace(item=SimpleNamespace(value=val))

    redactor = PiiRedactor(
        enable_cloud_dlp=True,
        gcp_project_id="demo-gcp-project",
        dlp_client=_MockDlpClient(),
    )
    scrubbed, modified = redactor.redact_text("Contact alice@example.com for status")
    assert modified is True
    assert "[EMAIL_ADDRESS]" in scrubbed
    assert len(calls) == 1
    assert calls[0]["parent"] == "projects/demo-gcp-project/locations/global"
