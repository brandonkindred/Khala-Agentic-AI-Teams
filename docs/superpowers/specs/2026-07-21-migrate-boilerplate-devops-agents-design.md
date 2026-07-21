# Migrate three boilerplate devops agents onto DevOpsSingleShotAgent

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Migrate `InfrastructureAsCodeAgent`, `CICDPipelineAgent`, and `DeploymentStrategyAgent` onto `DevOpsSingleShotAgent` as thin subclasses, preserving public class names, constructor/run contracts, prompts, and output-field mapping.

## Motivation

These three agents are pure boilerplate: build a context string, call `complete_json_with_continuation` with `temperature=0.1` / `think=True`, construct the output from `data.get(...)` defaults. The shared base already owns that scaffolding; migrating them first validates the base with the simplest consumers before the special-case agents.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Subclass shape | Inline `PROMPT` + `build_context` / `build_output` methods in each `agent.py` |
| Migration style | Direct rewrite of the three `agent.py` files only |
| Temperature / think | Inherit base defaults (`0.1` / `True`) — same as today |
| Test / monkeypatch changes | None — existing `_StubClient` and `_patch_fenced_response` paths still work |
| Scope | These three agents only |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `devops_team/iac_agent/agent.py` | `InfrastructureAsCodeAgent(DevOpsSingleShotAgent)` |
| `devops_team/cicd_pipeline_agent/agent.py` | `CICDPipelineAgent(DevOpsSingleShotAgent)` |
| `devops_team/deployment_strategy_agent/agent.py` | `DeploymentStrategyAgent(DevOpsSingleShotAgent)` |

### Files not touched

- `prompts.py`, `models.py`, package `__init__.py` for each agent
- `orchestrator.py`, `phase2_graph.py`
- `test_devops_team.py` and other test modules
- The four special-case agents (`infra_patch`, `infra_debug`, `devsecops_review`, `doc_runbook`)

### Per-agent contract

Each class:

1. Subclasses `DevOpsSingleShotAgent`
2. Sets `PROMPT` to the existing prompt constant from `prompts.py`
3. Implements `build_context(self, input_data) -> str` with the **same** f-string fields as today
4. Implements `build_output(self, input_data, data: dict)` with the **same** `data.get(...)` keys and defaults as today
5. Drops local imports of `resolve_strands_model`, `get_strands_model`, and `complete_json_with_continuation` (owned by the base)
6. Does not override `pre_call`, `temperature`, or `think`

Public surface stays:

- Class name unchanged
- `__init__(llm_client)` from the base (assert + `self.llm` + devops-keyed model)
- `run(input_data) -> Output` from the base

### Field mapping (must remain byte-identical)

**IaC** — context: `task_id`, `title`, `constraints`, `included`, `excluded`, `repo_summary`. Output: `artifacts` (or `{}`), `summary` (`""`), `plan_summary` (`""`), `destructive_changes_detected` (`bool`, default `False`), `blast_radius_notes` (or `[]`).

**CI/CD** — context: `task_id`, `title`, `environments`, `constraints`, `acceptance_criteria`, `existing_pipeline`. Output: `artifacts` (or `{}`), `pipeline_job_graph_summary` (`""`), `required_gates_present` (`bool`, default `False`), `summary` (`""`), `risks` (or `[]`).

**Deployment** — context: `task_id`, `constraints`, `environments`, `acceptance_criteria`, `nfr`. Output: `artifacts` (or `{}`), `strategy` (`""`), `rollback_plan` (or `[]`), `health_checks` (or `[]`), `rollout_timeout_minutes` (`int(data.get(..., 15) or 15)`), `summary` (`""`).

## Monkeypatchability

The base calls `complete_json_with_continuation` bound on `_agent_template`. Current tests for these three agents do **not** monkeypatch the per-agent module import; they use `_StubClient` or patch `shared.llm.Agent` via `_patch_fenced_response`. Therefore this migration makes **no** test-file edits. If a future test patches the helper by name, it must target `software_engineering_team.devops_team._agent_template.complete_json_with_continuation`.

## Testing

- Rely on existing `TestInfrastructureAsCodeAgent`, `TestCICDPipelineAgent`, `TestDeploymentStrategyAgent`, and the three fence-recovery tests for these agents in `test_devops_team.py`.
- Run focused pytest for those classes / fence tests plus `make lint` from `backend/`.
- Confirm ≥90% line coverage on each rewritten `agent.py`.

## Out of scope

- Migrating `infra_patch_agent`, `infra_debug_agent`, `devsecops_review_agent`, `doc_runbook_agent`
- Changing prompts, models, or orchestrator wiring
- Changing `DevOpsSingleShotAgent` itself

## Acceptance criteria mapping

| Criterion | How satisfied |
|---|---|
| Thin config-driven definition, no pre/post hooks | Subclass + `PROMPT` / `build_context` / `build_output` only |
| Public `__init__` / `run` / class names unchanged | Inherit base; keep class names |
| Monkeypatch targets updated if needed | N/A for current tests; documented for future |
| Output byte-identical for representative inputs | Same context strings and `data.get` defaults |
| `make test` / `make lint`; 90% on touched files | Implementation plan verifies |
