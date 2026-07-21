# DevOps single-shot migration closeout verification

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Confirm the completed `DevOpsSingleShotAgent` migration stack satisfies the parent standardization effort: all seven devops single-shot agents are thin subclasses of one shared base, `complete_json_with_continuation` is the documented canonical helper, and orchestrator / phase2 graph consumers still work. Deliverable is verification evidence plus issue closeout — not new agent code.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Closeout note location | PR body only (no separate closeout markdown artifact) |
| Branch base | `refactor/migrate-boilerplate-devops-agents` tip (all seven agents migrated) |
| Production code | No changes |
| PR keywords | Close both the closeout sub-issue and the parent standardization issue |
| Empty diff | Acceptable; evidence lives in the PR body |

## Verification checklist

1. Run full `test_devops_team.py` and `test_devops_debug_patch.py` — expect PASS.
2. Confirm orchestrator still constructs all seven agents (`iac_agent`, `cicd_agent`, `deployment_agent`, `devsecops_review_agent`, `doc_runbook_agent`, `infra_debug_agent`, `infra_patch_agent`) and `owner=` strings still name the public classes.
3. Confirm `phase2_graph.py` still imports `InfrastructureAsCodeAgent`, `CICDPipelineAgent`, `DeploymentStrategyAgent` and their Input/Output models.
4. Ruff check + format --check on migration-touched paths: `_agent_template.py`, the seven `*/agent.py` files, and `test_devops_agent_template.py`.
5. Coverage ≥90% on those same Python modules via a focused `--cov` run.
6. PR body closeout note stating: all seven agents are config-driven subclasses of `DevOpsSingleShotAgent`; canonical helper is `complete_json_with_continuation` (documented in `_agent_template.py`); paste command results.

## Out of scope

- New agent or template code
- Migrating agents outside `devops_team`
- Deprecating `run_structured_persona`
- Landing the stack on `main` as part of this closeout (separate from verification)

## Acceptance mapping

| Criterion | How satisfied |
|---|---|
| Full devops test suites pass | Checklist step 1 |
| Orchestrator / phase2 still resolve agents | Steps 2–3 + suite coverage |
| Lint clean on touched files | Step 4 |
| 90% coverage on touched files | Step 5 |
| Closeout confirms config-driven + canonical helper | Step 6 (PR body) |
| Parent issue closable | PR closes closeout + parent |
