"""Shared test fixtures isolating SQLite stores, redactors, and secret providers."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from app.observability.tracing import clear_fallback_recorded_spans
from app.security.pii_redactor import PiiRedactor, set_pii_redactor_for_testing
from app.security.secrets import SecretManagerProvider, set_secret_provider_for_testing
from app.tools.diagnostics_tools import set_runbook_store_for_testing
from app.tools.knowledge_store import PersistentRunbookVectorStore


@pytest.fixture(autouse=True)
def isolated_test_environment(tmp_path: Path) -> Iterator[None]:
    """Ensure every test uses clean temporary SQLite stores and local providers."""
    store = PersistentRunbookVectorStore(db_path=str(tmp_path / "runbooks.db"))
    set_runbook_store_for_testing(store)
    set_pii_redactor_for_testing(PiiRedactor(enable_cloud_dlp=False, gcp_project_id=""))
    set_secret_provider_for_testing(
        SecretManagerProvider(project_id="", fail_closed=False)
    )
    clear_fallback_recorded_spans()
    try:
        yield
    finally:
        store.close()
        set_runbook_store_for_testing(None)
        set_pii_redactor_for_testing(None)
        set_secret_provider_for_testing(None)
