"""Persistent SQLite-backed vector store for SRE incident runbooks & postmortems.

Provides deterministic, persistent cosine-similarity vector retrieval across
sessions and container restarts without requiring external infrastructure in
local/CI environments. When ``VERTEX_AI_SEARCH_DATASTORE_ID`` is configured in
Google Cloud, ``build_vertex_ai_search_tool`` also attaches ADK's managed
``VertexAiSearchTool`` for enterprise document grounding.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.memory.embeddings import compute_text_embedding, cosine_similarity
from app.schemas.models import RunbookHit


_SEED_RUNBOOKS: list[dict[str, Any]] = [
    {
        "runbook_id": "RB-CHECKOUT-001",
        "title": "Checkout API 503 Connection Pool Exhaustion After Release",
        "service_name": "checkout-api",
        "root_cause_pattern": (
            "HTTP 503 Service Unavailable and Cloud SQL connection pool timeout "
            "spiking immediately after a revision that reduced connection timeout "
            "or introduced an unclosed transaction leak."
        ),
        "mitigation_steps": [
            "Verify recent deployment revisions via query_recent_deployment_changes.",
            "Confirm connection pool exhaustion signatures in fetch_service_error_metrics.",
            "Request human confirmation to roll back checkout-api to last_known_good_revision_id.",
            "Create a SEV1/SEV2 incident ticket if SLO burn exceeds 5% error rate.",
        ],
        "requires_human_approval": True,
    },
    {
        "runbook_id": "RB-PAYMENTS-002",
        "title": "Payments Gateway Downstream Timeout & Circuit Breaker Trip",
        "service_name": "payments-gateway",
        "root_cause_pattern": (
            "Elevated p99 latency > 2000ms and gRPC DEADLINE_EXCEEDED errors caused "
            "by upstream traffic surge or stalled payment processor webhook retries."
        ),
        "mitigation_steps": [
            "Inspect upstream callers via inspect_service_dependency_topology.",
            "Scale payments-gateway replicas temporarily after human confirmation.",
            "Verify retry backoff settings and monitor error rate stabilization.",
        ],
        "requires_human_approval": True,
    },
    {
        "runbook_id": "RB-INVENTORY-003",
        "title": "Inventory Ledger Spanner Lock Contention & Hotspotting",
        "service_name": "inventory-ledger",
        "root_cause_pattern": (
            "ABORTED transaction spikes and elevated read-write lock wait times "
            "on hot SKU keys during flash-sale bursts."
        ),
        "mitigation_steps": [
            "Identify top hot-key error signatures in metrics.",
            "Scale read-only cache replicas or roll back recent schema/index release.",
            "Open SEV2 ticket for Database SRE review.",
        ],
        "requires_human_approval": True,
    },
    {
        "runbook_id": "RB-AUTH-004",
        "title": "Auth Token Service JWKS Key Rotation Failure",
        "service_name": "auth-token-service",
        "root_cause_pattern": (
            "HTTP 401 UNAUTHENTICATED surge across downstream APIs caused by stale "
            "JWKS cache or malformed Secret Manager signing key version."
        ),
        "mitigation_steps": [
            "Verify Secret Manager key version health (never log raw key material).",
            "Roll back auth-token-service revision if deployment coincided with 401 spike.",
            "Trigger graceful token cache refresh and alert Security On-Call.",
        ],
        "requires_human_approval": True,
    },
    {
        "runbook_id": "RB-SEARCH-005",
        "title": "Search Indexer OOMKilled & Embedding Queue Backlog",
        "service_name": "search-indexer",
        "root_cause_pattern": (
            "Container OOMKilled restarts and rising Pub/Sub dead-letter backlog "
            "due to oversized batch vectorization windows."
        ),
        "mitigation_steps": [
            "Check recent deployment changes for batch_size regressions.",
            "Scale Cloud Run worker replicas to drain backlog safely.",
            "Roll back batch_size configuration if OOM persists.",
        ],
        "requires_human_approval": False,
    },
]


class PersistentRunbookVectorStore:
    """SQLite-persisted runbook vector database for grounded SRE retrieval."""

    def __init__(self, db_path: str = "./data/sentinel_runbooks.db") -> None:
        self._db_path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()
        self._seed_defaults()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runbook_vectors (
                    runbook_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    service_name TEXT NOT NULL,
                    root_cause_pattern TEXT NOT NULL,
                    mitigation_steps_json TEXT NOT NULL,
                    requires_human_approval INTEGER NOT NULL,
                    embedding_json TEXT NOT NULL
                )
                """
            )

    def _seed_defaults(self) -> None:
        cursor = self._conn.execute("SELECT COUNT(*) FROM runbook_vectors")
        count = int(cursor.fetchone()[0])
        if count > 0:
            return
        for item in _SEED_RUNBOOKS:
            self.upsert_runbook(**item)

    def upsert_runbook(
        self,
        *,
        runbook_id: str,
        title: str,
        service_name: str,
        root_cause_pattern: str,
        mitigation_steps: list[str],
        requires_human_approval: bool,
    ) -> None:
        """Persist a runbook document and its vector embedding in SQLite."""
        searchable = (
            f"{title} {service_name} {root_cause_pattern} "
            + " ".join(mitigation_steps)
        )
        embedding = compute_text_embedding(searchable)
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO runbook_vectors (
                    runbook_id,
                    title,
                    service_name,
                    root_cause_pattern,
                    mitigation_steps_json,
                    requires_human_approval,
                    embedding_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    runbook_id,
                    title,
                    service_name,
                    root_cause_pattern,
                    json.dumps(mitigation_steps),
                    1 if requires_human_approval else 0,
                    json.dumps(embedding),
                ),
            )

    def search(
        self,
        *,
        query: str,
        service_name: str | None = None,
        top_k: int = 3,
    ) -> list[RunbookHit]:
        """Search persisted runbook embeddings by cosine similarity."""
        query_vec = compute_text_embedding(
            f"{query} {service_name or ''}".strip()
        )
        if service_name:
            rows = self._conn.execute(
                """
                SELECT runbook_id, title, service_name, root_cause_pattern,
                       mitigation_steps_json, requires_human_approval, embedding_json
                FROM runbook_vectors
                WHERE service_name = ?
                """,
                (service_name,),
            ).fetchall()
            # If filtered service yields nothing, broaden across all services.
            if not rows:
                rows = self._conn.execute(
                    """
                    SELECT runbook_id, title, service_name, root_cause_pattern,
                           mitigation_steps_json, requires_human_approval, embedding_json
                    FROM runbook_vectors
                    """
                ).fetchall()
        else:
            rows = self._conn.execute(
                """
                SELECT runbook_id, title, service_name, root_cause_pattern,
                       mitigation_steps_json, requires_human_approval, embedding_json
                FROM runbook_vectors
                """
            ).fetchall()

        scored: list[tuple[float, RunbookHit]] = []
        for row in rows:
            emb = json.loads(row[6])
            score = round(cosine_similarity(query_vec, emb), 4)
            # Small deterministic boost for exact service match.
            if service_name and row[2] == service_name:
                score = round(min(1.0, score + 0.15), 4)
            hit = RunbookHit(
                runbook_id=row[0],
                title=row[1],
                service_name=row[2],
                similarity_score=score,
                root_cause_pattern=row[3],
                mitigation_steps=json.loads(row[4]),
                requires_human_approval=bool(row[5]),
            )
            scored.append((score, hit))

        scored.sort(key=lambda item: item[0], reverse=True)
        return [hit for _, hit in scored[:top_k]]

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()


def build_vertex_ai_search_tool(datastore_id: str) -> Any | None:
    """Create ADK's ``VertexAiSearchTool`` when a Vertex AI Search datastore is configured."""
    if not datastore_id.strip():
        return None
    try:
        from google.adk.tools import VertexAiSearchTool

        return VertexAiSearchTool(data_store_id=datastore_id.strip())
    except ImportError:
        return None
