"""Context compaction, persistent sessions, and async memory consolidation."""

from __future__ import annotations

import importlib
from typing import Any

__all__ = [
    "AsyncMemoryConsolidationPlugin",
    "AsyncMemoryConsolidator",
    "CompactionResult",
    "PersistentMemoryRecord",
    "PersistentSqliteMemoryService",
    "SlidingWindowCompactor",
    "build_context_cache_config",
    "build_events_compaction_config",
    "compute_text_embedding",
    "cosine_similarity",
    "create_memory_service",
    "create_session_service",
    "estimate_token_count",
]

_EXPORTS: dict[str, tuple[str, str]] = {
    "AsyncMemoryConsolidationPlugin": (
        "app.memory.async_consolidator",
        "AsyncMemoryConsolidationPlugin",
    ),
    "AsyncMemoryConsolidator": (
        "app.memory.async_consolidator",
        "AsyncMemoryConsolidator",
    ),
    "CompactionResult": ("app.memory.compaction", "CompactionResult"),
    "SlidingWindowCompactor": (
        "app.memory.compaction",
        "SlidingWindowCompactor",
    ),
    "build_context_cache_config": (
        "app.memory.compaction",
        "build_context_cache_config",
    ),
    "build_events_compaction_config": (
        "app.memory.compaction",
        "build_events_compaction_config",
    ),
    "compute_text_embedding": (
        "app.memory.embeddings",
        "compute_text_embedding",
    ),
    "cosine_similarity": ("app.memory.embeddings", "cosine_similarity"),
    "estimate_token_count": ("app.memory.compaction", "estimate_token_count"),
    "PersistentMemoryRecord": (
        "app.memory.session_factory",
        "PersistentMemoryRecord",
    ),
    "PersistentSqliteMemoryService": (
        "app.memory.session_factory",
        "PersistentSqliteMemoryService",
    ),
    "create_memory_service": (
        "app.memory.session_factory",
        "create_memory_service",
    ),
    "create_session_service": (
        "app.memory.session_factory",
        "create_session_service",
    ),
}


def __getattr__(name: str) -> Any:
    if name in _EXPORTS:
        module_name, attr_name = _EXPORTS[name]
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
