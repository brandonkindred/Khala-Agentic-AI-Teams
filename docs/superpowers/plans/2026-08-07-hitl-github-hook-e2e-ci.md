# GitHub-hook WorkflowEnvironment CI Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing coding-team GitHub-hook `WorkflowEnvironment` tests in CI so a prep → pipeline → publish regression fails the SE Temporal integration step.

**Architecture:** Extend the SE `emit_shared_cov` Temporal integration pytest invocation in `.github/workflows/ci.yml` to include `test_coding_team_temporal_workflow.py` alongside the existing `test_code_review_temporal.py` file list. No production code and no new test logic unless the existing e2e fails locally.

**Tech Stack:** GitHub Actions, pytest, `temporalio.testing.WorkflowEnvironment`, existing `@pytest.mark.integration` markers.

**Spec:** `docs/superpowers/specs/2026-08-07-hitl-github-hook-e2e-ci-design.md`

## Global Constraints

- CI wiring only; do not add new activity-level tests or failure-path `WorkflowEnvironment` cases unless the existing happy-path e2e fails and needs a fix.
- Keep `@pytest.mark.integration` on the existing WorkflowEnvironment tests.
- Do not add `test_coding_team_resume_claim_integration.py` to the Temporal ephemeral-server pytest invocation (Postgres/claim; different job).
- Never reference GitHub issue numbers in code, comments, or commit messages.
- Work exclusively in the worktree `.worktrees/3999-hitl-github-hook-e2e-tests` on branch `feature/3999-hitl-github-hook-e2e-tests`.
- Prefer the main-repo venv when the worktree lacks one:
  `cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3999-hitl-github-hook-e2e-tests/backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest …`

---

## File map

| File | Responsibility |
|---|---|
| `.github/workflows/ci.yml` | SE Temporal integration pytest file list + comment |
| `backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py` | Existing e2e (read/verify only unless it fails) |

No new modules.

---

### Task 1: Baseline — existing GitHub WorkflowEnvironment e2e passes locally

**Files:**
- Test: `backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py` (read-only unless failure forces a fix)

**Interfaces:**
- Consumes: existing `test_workflow_github_path_prep_pipeline_publish`, `_workflow_environment_worker`
- Produces: confirmed local baseline before CI YAML change

- [ ] **Step 1: Run the happy-path GitHub WorkflowEnvironment test**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3999-hitl-github-hook-e2e-tests/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py::test_workflow_github_path_prep_pipeline_publish \
  -v --tb=short
```

Expected: `PASSED`, or `SKIPPED` only with message containing `Temporal ephemeral test server unavailable`.

- [ ] **Step 2: If the test FAILED (not skipped), stop and fix before touching CI**

Do not edit `.github/workflows/ci.yml` while the e2e is red. Diagnose against
`CodingTeamWorkflow.run`'s GitHub branch and the fakes in
`test_workflow_github_path_prep_pipeline_publish`. Re-run Step 1 until green
or intentionally skipped for missing binary.

- [ ] **Step 3: No commit for a pure baseline pass**

If Step 1 passed or skipped for missing binary, proceed to Task 2 with no commit.
Only commit in this task if you had to fix production/test code to make the e2e green.

```bash
# Only if a fix was required:
git add backend/agents/software_engineering_team/...
git commit -m "$(cat <<'EOF'
Fix coding-team GitHub WorkflowEnvironment e2e before CI wiring.

EOF
)"
```

---

### Task 2: Wire coding-team Temporal WorkflowEnvironment tests into CI

**Files:**
- Modify: `.github/workflows/ci.yml` (SE `emit_shared_cov` Temporal integration block, currently ~lines 907–915)

**Interfaces:**
- Consumes: existing CI pattern for `test_code_review_temporal.py -m integration`
- Produces: same invocation also collects `test_coding_team_temporal_workflow.py`

- [ ] **Step 1: Update the comment and pytest file list**

In `.github/workflows/ci.yml`, replace the Temporal integration comment + pytest
invocation inside the `elif [ -n "${{ matrix.emit_shared_cov }}" ]` branch with:

```yaml
            # Temporal WorkflowEnvironment tests (code-review + coding-team) are
            # marked ``integration`` so they stay out of the xdist unit suite
            # above (embedded Temporal test-server startup is heavier than a
            # pure unit case). They do not need Postgres/job-service — only the
            # ephemeral temporalio test binary — so run them here rather than
            # the Postgres integration job. Individual tests skip themselves
            # if that binary cannot be fetched. Do not add claim/Postgres
            # coding-team integration files here.
            pytest software_engineering_team/tests/test_code_review_temporal.py \
              software_engineering_team/tests/test_coding_team_temporal_workflow.py \
              -v --tb=short -m integration
```

Keep indentation identical to the surrounding `run: |` shell block (12 spaces
before `pytest`, continuation lines aligned with the existing style).

- [ ] **Step 2: Confirm the YAML still names only the two intended files**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3999-hitl-github-hook-e2e-tests
rg -n "test_coding_team_temporal_workflow|test_code_review_temporal|test_coding_team_resume_claim" .github/workflows/ci.yml
```

Expected:
- `test_code_review_temporal.py` present in the Temporal integration pytest line
- `test_coding_team_temporal_workflow.py` present in the same pytest line
- `test_coding_team_resume_claim` **absent** from `ci.yml`

- [ ] **Step 3: Run the exact CI marker command locally (coding-team file)**

Mirrors the new CI slice for the file this issue cares about (full
`test_code_review_temporal.py` integration suite is optional; it is already
proven in CI and is slow):

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3999-hitl-github-hook-e2e-tests/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py \
  -v --tb=short -m integration
```

Expected: both integration tests collected (`test_workflow_pauses_then_resumes_to_completion_via_signal` and `test_workflow_github_path_prep_pipeline_publish`); each `PASSED` or `SKIPPED` for missing Temporal binary; zero failures.

- [ ] **Step 4: Commit**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/3999-hitl-github-hook-e2e-tests
git add .github/workflows/ci.yml
git commit -m "$(cat <<'EOF'
Run coding-team Temporal WorkflowEnvironment tests in CI.

EOF
)"
```

---

## Spec coverage self-check

| Spec requirement | Task |
|---|---|
| CI placement in SE `emit_shared_cov` Temporal block | Task 2 |
| Add `test_coding_team_temporal_workflow.py` to pytest file list | Task 2 Step 1 |
| Update comment to cover code-review + coding-team | Task 2 Step 1 |
| Exclude claim/Postgres integration file | Task 2 Steps 1–2 |
| No new activity-level tests | Global constraints + Task 1 read-only |
| E2E must be runnable / fail on chain regression | Task 1 baseline + Task 2 Step 3 |
| Pause→resume sibling also runs as intentional side effect | Task 2 Step 3 collects both `-m integration` tests |
