"""Secure secret management backed by Google Cloud Secret Manager.

Enforces zero hardcoded API keys or credentials anywhere in the repository:
- In staging/production (or whenever ``GOOGLE_CLOUD_PROJECT`` is configured),
  retrieves versioned secrets via ``google.cloud.secretmanager.SecretManagerServiceClient``.
- Caches resolved secret payloads in memory with a bounded TTL to minimize RPC
  overhead while supporting rotation.
- Fails closed in production (``FAIL_CLOSED_SECRETS=true``) if a required secret
  cannot be retrieved from Secret Manager.
- Falls back to environment variables strictly for local development and tests.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from app.config import AppSettings, get_settings


class SecretAccessError(RuntimeError):
    """Raised when a required secret cannot be resolved securely."""


@dataclass(frozen=True)
class _CachedSecret:
    value: str
    expires_at: float


class SecretManagerProvider:
    """Versioned secret loader integrating Google Cloud Secret Manager."""

    def __init__(
        self,
        *,
        project_id: str | None = None,
        fail_closed: bool | None = None,
        cache_ttl_seconds: float = 300.0,
        client: Any | None = None,
    ) -> None:
        settings: AppSettings = get_settings()
        self._project_id = (
            project_id if project_id is not None else settings.gcp_project_id
        )
        self._fail_closed = (
            fail_closed
            if fail_closed is not None
            else settings.security.fail_closed_secrets
        )
        self._cache_ttl_seconds = cache_ttl_seconds
        self._client = client
        self._cache: dict[tuple[str, str], _CachedSecret] = {}

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from google.cloud import secretmanager

        self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def _build_resource_name(self, secret_id: str, version: str = "latest") -> str:
        return f"projects/{self._project_id}/secrets/{secret_id}/versions/{version}"

    def get_secret(
        self,
        secret_id: str,
        *,
        version: str = "latest",
        default_for_local: str | None = None,
    ) -> str:
        """Resolve a secret value from Secret Manager or local environment fallback.

        Args:
            secret_id: Secret Manager secret ID (and matching local env var name).
            version: Secret version identifier (defaults to ``'latest'``).
            default_for_local: Optional local-only mock default used strictly
                when ``fail_closed`` is False and neither Secret Manager nor
                an environment variable supplies the value.

        Returns:
            The resolved secret string (never logged by this provider).

        Raises:
            SecretAccessError: If the secret is missing or Secret Manager fails
                while ``fail_closed`` is enabled.
        """
        cache_key = (secret_id, version)
        now = time.monotonic()
        cached = self._cache.get(cache_key)
        if cached is not None and cached.expires_at > now:
            return cached.value

        # 1. Production / GCP path: Google Cloud Secret Manager
        if self._project_id:
            try:
                client = self._get_client()
                resource_name = self._build_resource_name(secret_id, version)
                response = client.access_secret_version(
                    request={"name": resource_name}
                )
                secret_value = response.payload.data.decode("utf-8").strip()
                if not secret_value:
                    raise SecretAccessError(
                        f"Secret '{secret_id}' version '{version}' is empty."
                    )
                self._cache[cache_key] = _CachedSecret(
                    value=secret_value,
                    expires_at=now + self._cache_ttl_seconds,
                )
                return secret_value
            except Exception as exc:
                if self._fail_closed:
                    raise SecretAccessError(
                        f"Failed to retrieve secret '{secret_id}' from Secret Manager "
                        f"(project={self._project_id}): {type(exc).__name__}"
                    ) from exc

        # 2. Local development fallback: environment variable injection
        env_val = os.getenv(secret_id, "").strip()
        if env_val:
            self._cache[cache_key] = _CachedSecret(
                value=env_val,
                expires_at=now + self._cache_ttl_seconds,
            )
            return env_val

        # 3. Fail closed in production or when no local mock default is permitted
        if self._fail_closed or default_for_local is None:
            raise SecretAccessError(
                f"Secret '{secret_id}' could not be resolved via Secret Manager "
                "or environment configuration."
            )

        return default_for_local

    def clear_cache(self) -> None:
        """Evict all cached secret versions (e.g., on forced key rotation)."""
        self._cache.clear()


_DEFAULT_PROVIDER: SecretManagerProvider | None = None


def get_secret_provider() -> SecretManagerProvider:
    """Return the process-wide ``SecretManagerProvider`` singleton."""
    global _DEFAULT_PROVIDER
    if _DEFAULT_PROVIDER is None:
        _DEFAULT_PROVIDER = SecretManagerProvider()
    return _DEFAULT_PROVIDER


def set_secret_provider_for_testing(
    provider: SecretManagerProvider | None,
) -> None:
    """Override the singleton secret provider during unit/integration tests."""
    global _DEFAULT_PROVIDER
    _DEFAULT_PROVIDER = provider
