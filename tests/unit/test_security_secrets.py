"""Unit tests for Secret Manager integration and static hygiene checks."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.security.secrets import SecretAccessError, SecretManagerProvider

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

_SECRET_LEAK_PATTERNS = (
    re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{24,}"),
)


def test_secret_manager_provider_fetches_and_caches_versioned_secret() -> None:
    calls: list[dict[str, Any]] = []

    class _MockSecretClient:
        def access_secret_version(self, *, request: dict[str, Any]) -> Any:
            calls.append(request)
            return SimpleNamespace(
                payload=SimpleNamespace(data=b"resolved-from-secret-manager")
            )

    provider = SecretManagerProvider(
        project_id="my-prod-project",
        fail_closed=True,
        client=_MockSecretClient(),
    )
    first = provider.get_secret("SENTINEL_PAGERDUTY_API_KEY", version="2")
    second = provider.get_secret("SENTINEL_PAGERDUTY_API_KEY", version="2")
    assert first == "resolved-from-secret-manager"
    assert second == first
    assert len(calls) == 1
    assert (
        calls[0]["name"]
        == "projects/my-prod-project/secrets/SENTINEL_PAGERDUTY_API_KEY/versions/2"
    )


def test_secret_manager_provider_fails_closed_in_production() -> None:
    class _FailingClient:
        def access_secret_version(self, *, request: dict[str, Any]) -> Any:
            del request
            raise PermissionError("IAM secretAccessor missing")

    provider = SecretManagerProvider(
        project_id="my-prod-project",
        fail_closed=True,
        client=_FailingClient(),
    )
    with pytest.raises(SecretAccessError):
        provider.get_secret(
            "SENTINEL_DEPLOY_API_TOKEN",
            default_for_local="must-not-be-used-when-fail-closed",
        )


def test_repository_contains_zero_hardcoded_api_keys_or_raw_print_calls() -> None:
    app_dir = REPO_ROOT / "app"
    for py_file in sorted(app_dir.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        for pat in _SECRET_LEAK_PATTERNS:
            assert not pat.search(source), f"Potential hardcoded secret in {py_file}"

        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                raise AssertionError(
                    f"Raw print() found in {py_file}:{node.lineno}; use structured logger instead."
                )
