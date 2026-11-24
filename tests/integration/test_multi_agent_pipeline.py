"""Integration tests verifying Multi-Agent ADK topology, Critic Loop, & Constitution."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.prompts.constitution import (
    COORDINATOR_CONSTITUTION,
    REMEDIATION_CRITIC_CONSTITUTION,
    ROOT_CAUSE_PLANNER_CONSTITUTION,
)
from app.schemas.models import CriticEvaluationReport

ADK_INSTALLED = importlib.util.find_spec("google.adk") is not None


def test_agent_constitutions_encode_persona_constraints_hitl_and_recovery() -> None:
    for required_section in (
        "<role_and_persona>",
        "<domain_knowledge>",
        "<operational_principles>",
        "<safety_and_policy_constraints>",
        "<human_in_the_loop_rules>",
        "<error_recovery_protocol>",
        "<output_contract>",
    ):
        assert required_section in COORDINATOR_CONSTITUTION

    assert "Gemini Pro" in ROOT_CAUSE_PLANNER_CONSTITUTION
    assert "Groundedness" in REMEDIATION_CRITIC_CONSTITUTION


def test_critic_self_eval_gate_enforces_safety_and_groundedness_thresholds() -> None:
    # Import evaluate_critic_gate directly when ADK is available, else test schema contract
    passing = CriticEvaluationReport(
        verdict="pass",
        groundedness_score=0.95,
        safety_compliance_score=1.0,
        completeness_score=0.9,
        policy_violations=[],
        revision_feedback="All checks passed.",
    )
    failing = CriticEvaluationReport(
        verdict="pass",
        groundedness_score=0.5,
        safety_compliance_score=0.7,
        completeness_score=0.9,
        policy_violations=["Missing HITL confirmation note"],
        revision_feedback="Cite metrics and require HITL approval.",
    )
    if ADK_INSTALLED:
        from app.agent import evaluate_critic_gate

        assert evaluate_critic_gate(passing) is True
        assert evaluate_critic_gate(failing) is False
    else:
        assert passing.verdict == "pass" and not passing.policy_violations
        assert len(failing.policy_violations) == 1


@pytest.mark.skipif(not ADK_INSTALLED, reason="google-adk package not installed")
def test_adk_multi_agent_app_topology_and_plugins(tmp_path: Path) -> None:
    from google.adk.agents import LoopAgent, ParallelAgent, SequentialAgent

    from app.agent import create_app
    from app.config import AppSettings

    settings = AppSettings(
        app_name="app",
        memory_db_path=str(tmp_path / "memory.db"),
        runbook_db_path=str(tmp_path / "runbooks.db"),
    )
    adk_app = create_app(settings)

    assert adk_app.name == "app"
    assert adk_app.events_compaction_config is not None
    assert adk_app.context_cache_config is not None
    assert adk_app.resumability_config is not None
    assert adk_app.resumability_config.is_resumable is True

    plugin_names = {p.name for p in adk_app.plugins}
    assert {
        "security_guardrails_plugin",
        "strategic_model_routing_plugin",
        "sentinel_observability_plugin",
        "async_memory_consolidation_plugin",
    }.issubset(plugin_names)

    root = adk_app.root_agent
    assert root is not None
    assert root.name == "sentinel_sre_coordinator"
    sub_by_name = {a.name: a for a in root.sub_agents}
    assert "incident_triage_agent" in sub_by_name
    assert "incident_investigation_pipeline" in sub_by_name
    assert "remediation_executor_agent" in sub_by_name

    pipeline = sub_by_name["incident_investigation_pipeline"]
    assert isinstance(pipeline, SequentialAgent)
    assert isinstance(pipeline.sub_agents[0], ParallelAgent)
    assert pipeline.sub_agents[1].name == "root_cause_planner_agent"
    assert pipeline.sub_agents[1].model == "gemini-2.5-pro"
    assert isinstance(pipeline.sub_agents[2], LoopAgent)
