"""Automated pytest regression suite against the golden evaluation dataset."""

from __future__ import annotations

import json

from tests.eval.run_eval import (
    EVAL_CONFIG_PATH,
    EVALSET_PATH,
    evaluate_golden_dataset_offline,
)


def test_eval_config_defines_mandatory_adk_criteria() -> None:
    cfg = json.loads(EVAL_CONFIG_PATH.read_text(encoding="utf-8"))
    criteria = cfg["criteria"]
    assert "tool_trajectory_avg_score" in criteria
    assert "response_match_score" in criteria
    assert "safety_v1" in criteria
    assert "rubric_based_final_response_quality_v1" in criteria
    assert "rubric_based_tool_use_quality_v1" in criteria


def test_golden_evalset_schema_and_coverage() -> None:
    evalset = json.loads(EVALSET_PATH.read_text(encoding="utf-8"))
    assert evalset["eval_set_id"] == "sentinel_sre_golden_v1"
    eval_ids = {case["eval_id"] for case in evalset["eval_cases"]}
    assert {
        "sev1_checkout_outage_investigation",
        "hitl_production_rollback_requires_confirmation",
        "guided_error_recovery_unknown_service",
        "security_prompt_injection_and_hitl_bypass_refusal",
        "pii_redaction_during_incident_ticket_creation",
    }.issubset(eval_ids)


def test_golden_eval_regression_suite_passes_100_percent() -> None:
    pass_rate, results = evaluate_golden_dataset_offline()
    failures = {r.eval_id: r.failures for r in results if not r.passed}
    assert pass_rate == 1.0, f"Golden eval regressions detected: {failures}"
