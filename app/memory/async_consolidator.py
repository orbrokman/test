"""Non-blocking asynchronous memory consolidation worker & ADK plugin.

Memory extraction, PII scrubbing, embedding generation, and Memory Bank writes
are expensive I/O operations. ``AsyncMemoryConsolidator`` offloads them into
tracked background ``asyncio.Task`` workers so user-facing agent responses
return immediately with zero UI blocking.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.memory.session_factory import PersistentSqliteMemoryService
from app.observability.logging import get_logger
from app.observability.tracing import get_tracer
from app.security.pii_redactor import PiiRedactor, get_pii_redactor

try:
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.plugins.base_plugin import BasePlugin
except ImportError:  # pragma: no cover - optional offline import guard
    InvocationContext = Any  # type: ignore[assignment,misc]

    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


logger = get_logger(__name__)
tracer = get_tracer(__name__)


class AsyncMemoryConsolidator:
    """Manages bounded, PII-scrubbed background memory consolidation tasks."""

    def __init__(
        self,
        memory_service: Any | None = None,
        redactor: PiiRedactor | None = None,
        max_concurrency: int = 4,
    ) -> None:
        self._memory_service = memory_service
        self._redactor = redactor or get_pii_redactor()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        # Strong task references prevent premature garbage collection per Python docs.
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        self.completed_count: int = 0
        self.failed_count: int = 0

    def schedule_session_consolidation(
        self,
        *,
        session: Any,
        memory_service: Any | None = None,
    ) -> asyncio.Task[Any]:
        """Fire-and-forget background task to consolidate a session into memory.

        Returns the scheduled ``asyncio.Task`` immediately without awaiting I/O.
        """
        target_service = memory_service or self._memory_service
        task = asyncio.create_task(
            self._consolidate_worker(session=session, memory_service=target_service),
            name=f"memory_consolidate_{getattr(session, 'id', 'session')}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        self._pending_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.failed_count += 1
            logger.error(
                "async_memory_consolidation_failed",
                task_name=task.get_name(),
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
        else:
            self.completed_count += 1

    async def _consolidate_worker(
        self,
        *,
        session: Any,
        memory_service: Any | None,
    ) -> None:
        """Run PII redaction and memory persistence under a bounded semaphore."""
        if memory_service is None:
            return

        session_id = getattr(session, "id", "unknown")
        user_id = getattr(session, "user_id", "unknown")
        app_name = getattr(session, "app_name", "app")

        async with self._semaphore:
            started = time.perf_counter()
            with tracer.start_as_current_span("memory.consolidate_async") as span:
                if hasattr(span, "set_attribute"):
                    span.set_attribute("sentinel.session_id", str(session_id))
                    span.set_attribute("sentinel.user_id", str(user_id))

                logger.info(
                    "async_memory_consolidation_started",
                    app_name=app_name,
                    session_id=session_id,
                    user_id=user_id,
                )

                # Scrub session text payloads before handing off to any remote Memory Bank.
                if isinstance(memory_service, PersistentSqliteMemoryService):
                    await memory_service.add_session_to_memory(session)
                else:
                    self._scrub_session_events_in_place(session)
                    await memory_service.add_session_to_memory(session)

                duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
                if hasattr(span, "set_attribute"):
                    span.set_attribute("sentinel.duration_ms", duration_ms)

                logger.info(
                    "async_memory_consolidation_completed",
                    app_name=app_name,
                    session_id=session_id,
                    user_id=user_id,
                    duration_ms=duration_ms,
                )

    def _scrub_session_events_in_place(self, session: Any) -> None:
        """Ensure raw PII is redacted from session event text before remote storage."""
        for event in getattr(session, "events", None) or []:
            content = getattr(event, "content", None)
            for part in getattr(content, "parts", None) or []:
                text = getattr(part, "text", None)
                if isinstance(text, str) and text:
                    clean, changed = self._redactor.redact_text(text)
                    if changed:
                        part.text = clean

    @property
    def pending_count(self) -> int:
        """Number of background consolidation tasks currently in flight."""
        return len(self._pending_tasks)

    async def drain(self, timeout_seconds: float = 10.0) -> None:
        """Wait for all in-flight background memory tasks to complete gracefully."""
        if not self._pending_tasks:
            return
        await asyncio.wait_for(
            asyncio.gather(*list(self._pending_tasks), return_exceptions=True),
            timeout=timeout_seconds,
        )


class AsyncMemoryConsolidationPlugin(BasePlugin):
    """ADK plugin scheduling non-blocking memory consolidation after each run."""

    def __init__(
        self,
        consolidator: AsyncMemoryConsolidator | None = None,
        name: str = "async_memory_consolidation_plugin",
    ) -> None:
        super().__init__(name=name)
        self.consolidator = consolidator or AsyncMemoryConsolidator()

    async def after_run_callback(
        self, *, invocation_context: InvocationContext
    ) -> None:
        """Dispatch background memory consolidation without blocking the response."""
        session = getattr(invocation_context, "session", None)
        memory_service = getattr(invocation_context, "memory_service", None)
        if session is None:
            return
        self.consolidator.schedule_session_consolidation(
            session=session,
            memory_service=memory_service,
        )

    async def close(self) -> None:
        """Flush in-flight memory consolidation tasks on graceful server shutdown."""
        await self.consolidator.drain()
