"""Context bloat management: ADK Event Compaction, Context Caching, and Sliding Windows.

Implements three complementary layers of context-window protection:
1. **ADK Sliding-Window & Token-Threshold Compaction** (``EventsCompactionConfig``
   with ``LlmEventSummarizer`` backed by Gemini Flash) to summarize older turns
   while retaining an overlap window for conversational continuity.
2. **Vertex AI / Gemini Context Caching** (``ContextCacheConfig``) to cache the
   shared system constitution and tool declarations across multi-turn sessions.
3. **Deterministic Token-Budget Sliding Window** (``SlidingWindowCompactor``)
   to cap oversized tool payloads and compact raw message histories deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.config import AppSettings, CompactionSettings

try:
    from google.adk.agents.context_cache_config import ContextCacheConfig
    from google.adk.apps.app import EventsCompactionConfig
    from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
    from google.adk.models import Gemini
except ImportError:  # pragma: no cover - optional offline import guard
    ContextCacheConfig = Any  # type: ignore[assignment,misc]
    EventsCompactionConfig = Any  # type: ignore[assignment,misc]
    LlmEventSummarizer = Any  # type: ignore[assignment,misc]
    Gemini = Any  # type: ignore[assignment,misc]


def estimate_token_count(text: str) -> int:
    """Fast heuristic token estimator (~4 characters per token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class CompactionResult:
    """Outcome of a deterministic sliding-window compaction pass."""

    messages: list[dict[str, str]]
    was_compacted: bool
    compacted_turn_count: int
    estimated_tokens_before: int
    estimated_tokens_after: int
    summary_text: str | None = None


class SlidingWindowCompactor:
    """Token-budget-aware sliding window compactor with overlap retention."""

    def __init__(
        self,
        *,
        max_tokens: int = 8000,
        keep_recent_turns: int = 4,
        max_tool_output_chars: int = 4000,
    ) -> None:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive.")
        if keep_recent_turns < 1:
            raise ValueError("keep_recent_turns must be >= 1.")
        self._max_tokens = max_tokens
        self._keep_recent_turns = keep_recent_turns
        self._max_tool_output_chars = max_tool_output_chars

    def truncate_tool_payload(self, payload_text: str) -> str:
        """Bound large diagnostic outputs so a single tool response never bloats context."""
        if len(payload_text) <= self._max_tool_output_chars:
            return payload_text
        head = self._max_tool_output_chars - 120
        omitted = len(payload_text) - head
        return (
            f"{payload_text[:head]}\n"
            f"... [TRUNCATED {omitted} chars to preserve context token budget]"
        )

    def compact_history(
        self, messages: list[dict[str, str]]
    ) -> CompactionResult:
        """Compact older conversation turns when estimated tokens exceed budget."""
        tokens_before = sum(
            estimate_token_count(m.get("content", "")) for m in messages
        )
        if (
            tokens_before <= self._max_tokens
            or len(messages) <= self._keep_recent_turns
        ):
            return CompactionResult(
                messages=list(messages),
                was_compacted=False,
                compacted_turn_count=0,
                estimated_tokens_before=tokens_before,
                estimated_tokens_after=tokens_before,
            )

        older = messages[: -self._keep_recent_turns]
        recent = messages[-self._keep_recent_turns :]

        summary_lines: list[str] = [
            f"[Compacted summary of {len(older)} earlier turns]:"
        ]
        for turn in older:
            role = turn.get("role", "unknown")
            snippet = turn.get("content", "").replace("\n", " ").strip()[:160]
            if snippet:
                summary_lines.append(f"- {role}: {snippet}")

        summary_text = "\n".join(summary_lines)
        compacted_messages = [
            {"role": "system", "content": summary_text},
            *recent,
        ]
        tokens_after = sum(
            estimate_token_count(m.get("content", ""))
            for m in compacted_messages
        )
        return CompactionResult(
            messages=compacted_messages,
            was_compacted=True,
            compacted_turn_count=len(older),
            estimated_tokens_before=tokens_before,
            estimated_tokens_after=tokens_after,
            summary_text=summary_text,
        )


def build_events_compaction_config(
    settings: AppSettings,
) -> EventsCompactionConfig:
    """Build ADK's ``EventsCompactionConfig`` using Gemini Flash as summarizer."""
    cfg: CompactionSettings = settings.compaction
    summarizer = LlmEventSummarizer(
        llm=Gemini(model=settings.models.summarizer_model),
        prompt_template=(
            "Summarize the SRE incident investigation history below concisely.\n"
            "CRITICAL: Preserve (1) affected service names, (2) exact metric values "
            "and revision IDs, (3) tool names invoked, and (4) any pending "
            "human-in-the-loop approval state. Never include raw PII or secrets.\n\n"
            "{conversation_history}"
        ),
    )
    return EventsCompactionConfig(
        summarizer=summarizer,
        compaction_interval=cfg.compaction_interval,
        overlap_size=cfg.overlap_size,
        token_threshold=cfg.token_threshold,
        event_retention_size=cfg.event_retention_size,
    )


def build_context_cache_config(settings: AppSettings) -> ContextCacheConfig:
    """Build ADK's ``ContextCacheConfig`` for Gemini prompt/context caching."""
    cfg: CompactionSettings = settings.compaction
    return ContextCacheConfig(
        min_tokens=cfg.context_cache_min_tokens,
        ttl_seconds=cfg.context_cache_ttl_seconds,
        cache_intervals=cfg.context_cache_intervals,
    )
