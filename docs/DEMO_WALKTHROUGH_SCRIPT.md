# Optional 3-Minute English Video Walkthrough Script

The lab allows an optional public English YouTube video demonstrating the problem, architecture, code walkthrough, and live agent execution. Here is a crisp 3-minute script you can follow if you choose to record one:

## 0:00 – 0:30 | Problem & Solution
- Introduce **Sentinel**, an Enterprise SRE Incident & Remediation Multi-Agent Copilot built with **Google ADK (Python)**.
- State the problem: during SEV1 outages, engineers manually jump across metrics, deployments, dependency graphs, and runbooks while risking unapproved production mutations or PII leaks.
- Show the architecture diagram in `README.md`.

## 0:30 – 1:15 | Multi-Agent Orchestration & Strategic Model Routing (`app/agent.py`, `app/routing/model_router.py`)
- Walk through the ADK agent hierarchy:
  - Coordinator (`sentinel_sre_coordinator`) + fast `incident_triage_agent` (Gemini 2.5 Flash)
  - `SequentialAgent` chaining a `ParallelAgent` (3 concurrent read-only Flash investigators) -> `root_cause_planner_agent` (Gemini 2.5 Pro) -> `LoopAgent` self-evaluation critic/refiner with custom `BaseAgent` `RemediationQualityEscalationChecker`
  - `remediation_executor_agent` isolated for HITL-guarded mutations
- Show `StrategicModelRoutingPlugin` dynamically escalating SEV1 requests from Flash to Pro.

## 1:15 – 2:00 | Tools, Schemas, Guardrails, HITL, & Memory
- Show `app/schemas/models.py` (strict Pydantic v2 JSON schemas with `extra="forbid"`) and `app/tools/diagnostics_tools.py` (descriptive names, Google-style docstrings, `ToolErrorResponse` guided recovery).
- Show `app/tools/remediation_tools.py` (`FunctionTool(require_confirmation=...)` + `tool_context.request_confirmation(...)` stopping before production rollbacks).
- Show `app/memory/compaction.py` (`EventsCompactionConfig` + `ContextCacheConfig`) and `app/memory/async_consolidator.py` (non-blocking background `asyncio.create_task` writing PII-scrubbed memories to Vertex AI Memory Bank / SQLite).

## 2:00 – 2:40 | Observability, PII Redaction, Secret Manager, & IaC
- Show `app/security/pii_redactor.py` (Google Cloud DLP + deterministic fallback), `app/security/guardrails_plugin.py` (Model Armor + prompt injection + RBAC), and `app/security/secrets.py` (Secret Manager fail-closed provider).
- Show `app/observability/observability_plugin.py` (structured JSON Intent-vs-Outcome logs correlated by `action_correlation_id` and OpenTelemetry spans).
- Briefly scroll through `deployment/terraform/` and `.github/workflows/ci.yml`.

## 2:40 – 3:00 | Live Demo & Automated Golden Eval
- Run `pytest` and `python -m tests.eval.run_eval --mode offline` (or `adk web .` with the prompt: *"URGENT SEV1: checkout-api in production is throwing HTTP 503 errors after the latest release. Investigate root cause, propose a remediation plan, and prepare a rollback."*).
- Highlight the HITL confirmation pause, structured JSON logs, and passing 100% golden evaluation suite.
