# Design: Run GitHub-hook WorkflowEnvironment tests in CI

Date: 2026-08-07

## Goal

Satisfy the GitHub-hook e2e verification acceptance criterion: the existing
`WorkflowEnvironment` test for the coding-team `run-from-github` path
(branch prep → pipeline → publish) must execute in CI, and fail CI if that
chain regresses.

## Context

Part of the coding-team HITL Temporal redesign (parent epic). Dependency
"wire CodingTeamWorkflow to call GitHub activities" is already landed
(PR that added `request["github"]` handling and
`test_workflow_github_path_prep_pipeline_publish`).

Already present on main:

- Activity-level unit tests for branch-prep, publish, and failure-notice
  (covered by their own sub-issues) — no further activity tests needed.
- Monkeypatch workflow-shape tests for happy and failure GitHub paths in
  `test_coding_team_temporal_workflow.py`.
- `WorkflowEnvironment` happy-path test
  `test_workflow_github_path_prep_pipeline_publish` (plus the sibling
  pause → signal → resume harness test in the same file).

Gap: those `WorkflowEnvironment` tests are `@pytest.mark.integration`. SE's
unit job runs `-m "not integration"`, and the follow-on Temporal integration
invocation only targets `test_code_review_temporal.py`. The GitHub e2e test
therefore never runs in CI today.

Out of scope: new failure-path `WorkflowEnvironment` cases, claim/Postgres
integration files (e.g. `test_coding_team_resume_claim_integration.py`),
and manual/browser verification.

## Decisions

| Topic | Choice |
|---|---|
| Scope | CI wiring only; no new production or test logic unless the existing e2e fails |
| CI placement | Same SE `emit_shared_cov` branch that already runs code-review Temporal integration tests |
| Files | Add `software_engineering_team/tests/test_coding_team_temporal_workflow.py` alongside `test_code_review_temporal.py` in one `pytest … -m integration` invocation |
| Marker | Keep `@pytest.mark.integration` (embedded Temporal test server is heavier than unit xdist) |
| Skip behavior | Unchanged: helper skips if the ephemeral Temporal binary cannot be fetched |
| Explicitly excluded | `test_coding_team_resume_claim_integration.py` (claim/Postgres; different job) |
| Activity unit tests | No changes (already covered by sub-issues) |

## Behavior

In `.github/workflows/ci.yml`, inside the SE `emit_shared_cov` step, change
the Temporal integration pytest call from a single file to:

```bash
pytest software_engineering_team/tests/test_code_review_temporal.py \
  software_engineering_team/tests/test_coding_team_temporal_workflow.py \
  -v --tb=short -m integration
```

Update the adjacent comment so it refers to Temporal `WorkflowEnvironment`
tests generally (code-review + coding-team), not only code-review.

Side effect (intentional): the pause → signal → resume
`WorkflowEnvironment` test in the same coding-team file also starts running
in CI under the same harness.

## Acceptance

- On an SE-triggered CI run, `test_workflow_github_path_prep_pipeline_publish`
  executes (does not remain excluded by `-m "not integration"` alone).
- A regression that breaks the prep → pipeline → publish activity chain
  fails that CI step.
- No new activity-level test files are required for this leaf issue.
