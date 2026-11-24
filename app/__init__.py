"""Sentinel SRE Multi-Agent ADK application package.

Exposes ``app`` and ``root_agent`` for standard ADK CLI discovery
(``adk web``, ``adk run``, ``adk eval``) while remaining importable in
lightweight unit-test environments.
"""

from __future__ import annotations

from typing import Any

__all__ = ["app", "root_agent"]


def __getattr__(name: str) -> Any:
    if name in ("app", "root_agent"):
        from app import agent as _agent_module

        return getattr(_agent_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
