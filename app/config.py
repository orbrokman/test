"""Application configuration and strategic model routing settings.

All sensitive values (API keys, PagerDuty tokens, database passwords) are
NEVER hardcoded here. Production deployments resolve them through
``app.security.secrets.SecretManagerProvider`` backed by Google Cloud
Secret Manager, with environment variable fallback strictly for local dev.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

Environment = Literal["local", "staging", "production"]
SessionBackend = Literal["vertex_ai", "database", "sqlite", "memory"]
MemoryBackend = Literal["vertex_ai_memory_bank", "sqlite", "in_memory"]


@dataclass(frozen=True)
class ModelRoutingConfig:
    """Declarative mapping of task tiers to Gemini model identifiers.

    - ``fast_model``: Low-latency, cost-efficient model (Gemini Flash) for
      triage, log/metric tool calls, runbook retrieval, event compaction
      summarization, and fast policy checks.
    - ``reasoning_model``: High-capability model (Gemini Pro) for complex
      multi-signal root-cause synthesis, remediation planning, P0/P1 critical
      incident analysis, and high-stakes safety review.
    """

    fast_model: str = "gemini-2.5-flash"
    reasoning_model: str = "gemini-2.5-pro"
    summarizer_model: str = "gemini-2.5-flash"
    judge_model: str = "gemini-2.5-flash"
    complexity_escalation_chars: int = 1200


@dataclass(frozen=True)
class CompactionSettings:
    """Context window and conversation history compaction settings."""

    compaction_interval: int = 6
    overlap_size: int = 2
    token_threshold: int = 24000
    event_retention_size: int = 4
    context_cache_min_tokens: int = 2048
    context_cache_ttl_seconds: int = 1800
    context_cache_intervals: int = 10
    max_tool_output_chars: int = 4000


@dataclass(frozen=True)
class SecuritySettings:
    """Security, Secret Manager, Cloud DLP, and Model Armor settings."""

    enable_cloud_dlp: bool = False
    dlp_inspect_template: str = ""
    dlp_deidentify_template: str = ""
    enable_model_armor: bool = False
    model_armor_prompt_template: str = ""
    model_armor_response_template: str = ""
    fail_closed_secrets: bool = False
    production_scale_confirmation_threshold: int = 10


@dataclass(frozen=True)
class ObservabilitySettings:
    """Structured JSON logging and OpenTelemetry tracing settings."""

    service_name: str = "sentinel-sre-agent"
    service_version: str = "1.0.0"
    log_level: str = "INFO"
    enable_cloud_trace: bool = False
    otlp_endpoint: str = ""
    capture_message_content: str = "NO_CONTENT"
    bq_analytics_dataset: str = "sentinel_sre_analytics"


@dataclass(frozen=True)
class AppSettings:
    """Top-level runtime configuration loaded from environment variables."""

    app_name: str = "app"
    environment: Environment = "local"
    gcp_project_id: str = ""
    gcp_location: str = "us-central1"
    use_vertex_ai: bool = True
    agent_engine_id: str = ""
    vertex_ai_search_datastore_id: str = ""
    session_backend: SessionBackend = "sqlite"
    session_db_url: str = "sqlite+aiosqlite:///./data/sentinel_sessions.db"
    memory_backend: MemoryBackend = "sqlite"
    memory_db_path: str = "./data/sentinel_memory.db"
    runbook_db_path: str = "./data/sentinel_runbooks.db"
    models: ModelRoutingConfig = field(default_factory=ModelRoutingConfig)
    compaction: CompactionSettings = field(default_factory=CompactionSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    observability: ObservabilitySettings = field(
        default_factory=ObservabilitySettings
    )

    @classmethod
    def from_env(cls) -> AppSettings:
        """Build settings from environment variables without exposing secrets."""

        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return int(raw.strip())
            except ValueError:
                return default

        env_raw = os.getenv("SENTINEL_ENV", "local").strip().lower()
        environment: Environment = (
            env_raw
            if env_raw in ("local", "staging", "production")
            else "local"
        )

        session_raw = os.getenv("SESSION_BACKEND", "sqlite").strip().lower()
        session_backend: SessionBackend = (
            session_raw
            if session_raw in ("vertex_ai", "database", "sqlite", "memory")
            else "sqlite"
        )

        memory_raw = os.getenv("MEMORY_BACKEND", "sqlite").strip().lower()
        memory_backend: MemoryBackend = (
            memory_raw
            if memory_raw in ("vertex_ai_memory_bank", "sqlite", "in_memory")
            else "sqlite"
        )

        return cls(
            app_name=os.getenv("ADK_APP_NAME", "app"),
            environment=environment,
            gcp_project_id=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            use_vertex_ai=_bool("GOOGLE_GENAI_USE_VERTEXAI", True),
            agent_engine_id=os.getenv("VERTEX_AGENT_ENGINE_ID", ""),
            vertex_ai_search_datastore_id=os.getenv(
                "VERTEX_AI_SEARCH_DATASTORE_ID", ""
            ),
            session_backend=session_backend,
            session_db_url=os.getenv(
                "SESSION_DB_URL",
                "sqlite+aiosqlite:///./data/sentinel_sessions.db",
            ),
            memory_backend=memory_backend,
            memory_db_path=os.getenv(
                "MEMORY_DB_PATH", "./data/sentinel_memory.db"
            ),
            runbook_db_path=os.getenv(
                "RUNBOOK_DB_PATH", "./data/sentinel_runbooks.db"
            ),
            models=ModelRoutingConfig(
                fast_model=os.getenv("SENTINEL_FAST_MODEL", "gemini-2.5-flash"),
                reasoning_model=os.getenv(
                    "SENTINEL_REASONING_MODEL", "gemini-2.5-pro"
                ),
                summarizer_model=os.getenv(
                    "SENTINEL_SUMMARIZER_MODEL", "gemini-2.5-flash"
                ),
                judge_model=os.getenv("SENTINEL_JUDGE_MODEL", "gemini-2.5-flash"),
                complexity_escalation_chars=_int(
                    "MODEL_ROUTING_COMPLEXITY_CHARS", 1200
                ),
            ),
            compaction=CompactionSettings(
                compaction_interval=_int("COMPACTION_INTERVAL", 6),
                overlap_size=_int("COMPACTION_OVERLAP_SIZE", 2),
                token_threshold=_int("COMPACTION_TOKEN_THRESHOLD", 24000),
                event_retention_size=_int("COMPACTION_EVENT_RETENTION_SIZE", 4),
                context_cache_min_tokens=_int("CONTEXT_CACHE_MIN_TOKENS", 2048),
                context_cache_ttl_seconds=_int("CONTEXT_CACHE_TTL_SECONDS", 1800),
                context_cache_intervals=_int("CONTEXT_CACHE_INTERVALS", 10),
                max_tool_output_chars=_int("MAX_TOOL_OUTPUT_CHARS", 4000),
            ),
            security=SecuritySettings(
                enable_cloud_dlp=_bool("ENABLE_CLOUD_DLP", False),
                dlp_inspect_template=os.getenv("DLP_INSPECT_TEMPLATE", ""),
                dlp_deidentify_template=os.getenv("DLP_DEIDENTIFY_TEMPLATE", ""),
                enable_model_armor=_bool("ENABLE_MODEL_ARMOR", False),
                model_armor_prompt_template=os.getenv(
                    "MODEL_ARMOR_PROMPT_TEMPLATE", ""
                ),
                model_armor_response_template=os.getenv(
                    "MODEL_ARMOR_RESPONSE_TEMPLATE", ""
                ),
                fail_closed_secrets=_bool(
                    "FAIL_CLOSED_SECRETS", environment == "production"
                ),
                production_scale_confirmation_threshold=_int(
                    "SCALE_CONFIRMATION_THRESHOLD", 10
                ),
            ),
            observability=ObservabilitySettings(
                service_name=os.getenv("OTEL_SERVICE_NAME", "sentinel-sre-agent"),
                service_version=os.getenv("OTEL_SERVICE_VERSION", "1.0.0"),
                log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
                enable_cloud_trace=_bool("ENABLE_CLOUD_TRACE", False),
                otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
                capture_message_content=os.getenv(
                    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT",
                    "NO_CONTENT",
                ),
                bq_analytics_dataset=os.getenv(
                    "BQ_ANALYTICS_DATASET_ID", "sentinel_sre_analytics"
                ),
            ),
        )


def get_settings() -> AppSettings:
    """Return current runtime settings loaded from environment."""
    return AppSettings.from_env()
