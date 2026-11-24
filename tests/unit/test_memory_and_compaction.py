"""Unit tests for History Compaction, Persistent SQLite Stores, & Async Memory Operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.memory.async_consolidator import AsyncMemoryConsolidator
from app.memory.compaction import SlidingWindowCompactor
from app.memory.session_factory import PersistentSqliteMemoryService
from app.tools.knowledge_store import PersistentRunbookVectorStore


def test_sliding_window_compactor_reduces_token_bloat_and_truncates_tool_payloads() -> None:
    compactor = SlidingWindowCompactor(
        max_tokens=120,
        keep_recent_turns=2,
        max_tool_output_chars=250,
    )
    history = [
        {"role": "user", "content": f"Turn {i}: " + ("diagnostic log detail " * 15)}
        for i in range(8)
    ]
    result = compactor.compact_history(history)
    assert result.was_compacted is True
    assert result.compacted_turn_count == 6
    assert len(result.messages) == 3  # 1 summary + 2 retained turns
    assert result.estimated_tokens_after < result.estimated_tokens_before

    huge_output = "X" * 1000
    truncated = compactor.truncate_tool_payload(huge_output)
    assert len(truncated) < 300
    assert "TRUNCATED" in truncated


def test_persistent_vector_runbook_store_survives_restarts(tmp_path: Path) -> None:
    db_file = str(tmp_path / "persisted_runbooks.db")
    store1 = PersistentRunbookVectorStore(db_path=db_file)
    store1.upsert_runbook(
        runbook_id="RB-CUSTOM-999",
        title="Custom Redis Cluster Failover Split-Brain Recovery",
        service_name="checkout-api",
        root_cause_pattern="Redis cluster quorum lost during zonal network partition",
        mitigation_steps=["Verify sentinel quorum", "Promote healthy primary replica"],
        requires_human_approval=True,
    )
    store1.close()

    # Re-open from disk to prove durability across process restarts
    store2 = PersistentRunbookVectorStore(db_path=db_file)
    hits = store2.search(
        query="Redis cluster split-brain quorum partition",
        service_name="checkout-api",
        top_k=2,
    )
    store2.close()
    assert hits[0].runbook_id == "RB-CUSTOM-999"


def test_async_memory_consolidation_is_non_blocking_and_scrubs_pii(tmp_path: Path) -> None:
    db_file = str(tmp_path / "memory_bank.db")
    memory_service = PersistentSqliteMemoryService(db_path=db_file)

    async def _run() -> None:
        consolidator = AsyncMemoryConsolidator(memory_service=memory_service)
        fake_session = SimpleNamespace(
            app_name="app",
            user_id="oncall-sre-1",
            id="sess-42",
            state={"triage_report": {"service_name": "checkout-api", "severity": "SEV1"}},
            events=[
                SimpleNamespace(
                    author="user",
                    content=SimpleNamespace(
                        parts=[
                            SimpleNamespace(
                                text=(
                                    "checkout-api 503 outage escalated by "
                                    "jane.sre@example.com SSN 123-45-6789."
                                )
                            )
                        ]
                    ),
                )
            ],
        )

        # Scheduling returns immediately as an asyncio.Task without blocking UI
        task = consolidator.schedule_session_consolidation(session=fake_session)
        assert isinstance(task, asyncio.Task)
        await consolidator.drain(timeout_seconds=5.0)
        assert consolidator.completed_count == 1
        assert consolidator.failed_count == 0

        found = await memory_service.search_memory(
            app_name="app",
            user_id="oncall-sre-1",
            query="checkout-api 503 outage",
        )
        assert len(found["memories"]) == 1
        record = found["memories"][0]
        assert "jane.sre@example.com" not in record.summary
        assert "123-45-6789" not in record.summary
        assert "[REDACTED_EMAIL]" in record.summary
        assert "[REDACTED_SSN]" in record.summary

    try:
        asyncio.run(_run())
    finally:
        memory_service.close()
