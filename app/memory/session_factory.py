"""Persistent session state and long-term memory service factories.

Supports seamless switching between local persistent SQLite storage and
Google Cloud managed backends without code changes:
- **Sessions**: ``VertexAiSessionService`` (Vertex AI Agent Engine) or
  ``DatabaseSessionService`` (Cloud SQL PostgreSQL / local SQLite).
- **Long-Term Memory**: ``VertexAiMemoryBankService`` (Vertex AI Memory Bank)
  or ``PersistentSqliteMemoryService`` (persistent SQLite + vector cosine search).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import AppSettings
from app.memory.embeddings import compute_text_embedding, cosine_similarity
from app.security.pii_redactor import PiiRedactor, get_pii_redactor


@dataclass(frozen=True)
class PersistentMemoryRecord:
    """Single cross-session memory entry stored after PII redaction."""

    memory_id: str
    app_name: str
    user_id: str
    session_id: str
    summary: str
    tags: list[str]
    created_at_utc: str
    similarity_score: float = 1.0


class PersistentSqliteMemoryService:
    """SQLite-persisted long-term memory store with vector similarity retrieval.

    Implements the same async surface as ADK's ``BaseMemoryService``
    (``add_session_to_memory`` and ``search_memory``) while ensuring every
    persisted record passes through ``PiiRedactor`` before hitting disk.
    """

    def __init__(
        self,
        db_path: str = "./data/sentinel_memory.db",
        redactor: PiiRedactor | None = None,
    ) -> None:
        self._db_path = db_path
        self._redactor = redactor or get_pii_redactor()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS episodic_memories (
                    memory_id TEXT PRIMARY KEY,
                    app_name TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    tags_json TEXT NOT NULL,
                    embedding_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                )
                """
            )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memories_scope
                ON episodic_memories (app_name, user_id)
                """
            )

    async def add_memory_record(
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        raw_text: str,
        tags: list[str] | None = None,
    ) -> PersistentMemoryRecord:
        """Scrub PII and persist a consolidated memory entry to SQLite."""
        scrubbed_text, _ = self._redactor.redact_text(raw_text)
        memory_id = f"mem-{uuid.uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()
        clean_tags = sorted({t.strip().lower() for t in (tags or []) if t.strip()})
        embedding = compute_text_embedding(
            f"{scrubbed_text} {' '.join(clean_tags)}"
        )

        with self._conn:
            self._conn.execute(
                """
                INSERT INTO episodic_memories (
                    memory_id, app_name, user_id, session_id,
                    summary, tags_json, embedding_json, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    app_name,
                    user_id,
                    session_id,
                    scrubbed_text,
                    json.dumps(clean_tags),
                    json.dumps(embedding),
                    created_at,
                ),
            )
        return PersistentMemoryRecord(
            memory_id=memory_id,
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            summary=scrubbed_text,
            tags=clean_tags,
            created_at_utc=created_at,
        )

    async def add_session_to_memory(self, session: Any) -> PersistentMemoryRecord | None:
        """Extract salient text from an ADK Session, scrub PII, and persist."""
        app_name = getattr(session, "app_name", "app")
        user_id = getattr(session, "user_id", "unknown")
        session_id = getattr(session, "id", "unknown")

        snippets: list[str] = []
        for event in getattr(session, "events", None) or []:
            content = getattr(event, "content", None)
            for part in getattr(content, "parts", None) or []:
                text = getattr(part, "text", None)
                if text:
                    author = getattr(event, "author", "agent")
                    snippets.append(f"{author}: {text[:240]}")

        state = getattr(session, "state", None) or {}
        tags: list[str] = []
        triage = state.get("triage_report")
        if isinstance(triage, dict):
            svc = triage.get("service_name")
            sev = triage.get("severity")
            if svc:
                tags.append(str(svc))
            if sev:
                tags.append(str(sev))

        if not snippets:
            return None

        consolidated = " | ".join(snippets[-8:])
        return await self.add_memory_record(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
            raw_text=consolidated,
            tags=tags,
        )

    async def search_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        query: str,
        top_k: int = 5,
    ) -> dict[str, Any]:
        """Retrieve top-k semantically similar memories for ``(app_name, user_id)``."""
        query_vec = compute_text_embedding(query)
        rows = self._conn.execute(
            """
            SELECT memory_id, app_name, user_id, session_id,
                   summary, tags_json, embedding_json, created_at_utc
            FROM episodic_memories
            WHERE app_name = ? AND user_id = ?
            """,
            (app_name, user_id),
        ).fetchall()

        scored: list[tuple[float, PersistentMemoryRecord]] = []
        for row in rows:
            emb = json.loads(row[6])
            score = round(cosine_similarity(query_vec, emb), 4)
            record = PersistentMemoryRecord(
                memory_id=row[0],
                app_name=row[1],
                user_id=row[2],
                session_id=row[3],
                summary=row[4],
                tags=json.loads(row[5]),
                created_at_utc=row[7],
                similarity_score=score,
            )
            scored.append((score, record))

        scored.sort(key=lambda item: item[0], reverse=True)
        return {"memories": [rec for _, rec in scored[:top_k]]}

    def count_memories(self, *, app_name: str, user_id: str) -> int:
        """Count persisted memories for a user."""
        row = self._conn.execute(
            """
            SELECT COUNT(*) FROM episodic_memories
            WHERE app_name = ? AND user_id = ?
            """,
            (app_name, user_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the SQLite database connection."""
        self._conn.close()


def create_session_service(settings: AppSettings) -> Any:
    """Instantiate the configured ADK persistent session service backend."""
    if settings.session_backend == "vertex_ai":
        from google.adk.sessions import VertexAiSessionService

        return VertexAiSessionService(
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            agent_engine_id=settings.agent_engine_id or None,
        )

    if settings.session_backend in ("database", "sqlite"):
        from google.adk.sessions import DatabaseSessionService

        if settings.session_db_url.startswith("sqlite"):
            # Ensure parent directory exists for file-backed SQLite DBs.
            path_part = settings.session_db_url.split("///", 1)[-1]
            if path_part and path_part != ":memory:":
                Path(path_part).parent.mkdir(parents=True, exist_ok=True)
        return DatabaseSessionService(db_url=settings.session_db_url)

    from google.adk.sessions import InMemorySessionService

    return InMemorySessionService()


def create_memory_service(settings: AppSettings) -> Any:
    """Instantiate Vertex AI Memory Bank (prod) or PersistentSqliteMemoryService."""
    if (
        settings.memory_backend == "vertex_ai_memory_bank"
        and settings.agent_engine_id
    ):
        from google.adk.memory import VertexAiMemoryBankService

        return VertexAiMemoryBankService(
            project=settings.gcp_project_id,
            location=settings.gcp_location,
            agent_engine_id=settings.agent_engine_id,
        )

    if settings.memory_backend == "in_memory":
        from google.adk.memory import InMemoryMemoryService

        return InMemoryMemoryService()

    return PersistentSqliteMemoryService(db_path=settings.memory_db_path)
