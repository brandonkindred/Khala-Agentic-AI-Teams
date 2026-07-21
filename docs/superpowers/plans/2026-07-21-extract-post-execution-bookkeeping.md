# Extract Post-Execution Bookkeeping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the duplicated post-execution bookkeeping block from backend/frontend `run_workflow` onto `BaseV2DevelopmentAgent._record_execution_bookkeeping`.

**Architecture:** Add one static helper on the shared v2 base that counts completed/review-failed microtasks by string status value, sets `result.iterations_used`, optionally calls `commit_current_changes`, and returns `(completed_count, failed_count)`. Both team orchestrators call it; `result.final_files` and deliver/logging stay per-team.

**Tech Stack:** Python 3.10+, pytest, ruff, existing `BaseV2DevelopmentAgent`.

**Spec:** `docs/superpowers/specs/2026-07-21-extract-post-execution-bookkeeping-design.md`

## Global Constraints

- Touch only the four files in the file map (plus this plan's commits).
- Method name must be `_record_execution_bookkeeping`.
- Compare microtask status by string value (`"completed"` / `"review_failed"`); do not import team `MicrotaskStatus` enums into the base.
- Preserve today's commit guards and warning-log message shape.
- Do not move `result.final_files = current_files` into the helper.
- Existing `test_backend_code_v2_team.py` / `test_frontend_code_v2_team.py` must pass unchanged (no edits).
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- DbC: `Preconditions:` / `Postconditions:` on the new helper docstring.
- ≥90% line coverage on touched files; `make lint` must pass from `backend/`.

## File map

| Path | Responsibility after change |
|---|---|
| `backend/agents/software_engineering_team/shared/v2_orchestrator.py` | Owns `_record_execution_bookkeeping` |
| `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py` | Calls the helper; drops unused `MicrotaskStatus` import |
| `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py` | Calls the helper; drops unused `MicrotaskStatus` import |
| `backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py` | Unit tests for the helper |

---

### Task 1: Add helper + unit tests (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py`
- Modify: `backend/agents/software_engineering_team/shared/v2_orchestrator.py`

**Interfaces:**
- Consumes: `exec_result.microtasks` items with `.status`; mutable `result.iterations_used`; optional `git_agent.commit_current_changes`
- Produces: `BaseV2DevelopmentAgent._record_execution_bookkeeping(...) -> Tuple[int, int]`

- [ ] **Step 1: Write the failing tests**

Append to `test_v2_orchestrator_helpers.py` (after `TestRunPlanningAndBranchSetup`):

```python
class _FakeMicrotask:
    def __init__(self, status: str):
        self.status = status


class _FakeExecResult:
    def __init__(self, statuses: list[str]):
        self.microtasks = [_FakeMicrotask(s) for s in statuses]


class _FakeWorkflowResult:
    def __init__(self):
        self.iterations_used = 0


class TestRecordExecutionBookkeeping:
    """Tests for ``BaseV2DevelopmentAgent._record_execution_bookkeeping``."""

    @staticmethod
    def _logger():
        import logging

        return logging.getLogger("test_record_execution_bookkeeping")

    def test_counts_sets_iterations_and_commits(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        result = _FakeWorkflowResult()
        exec_result = _FakeExecResult(
            ["completed", "completed", "review_failed", "pending"]
        )

        completed_count, failed_count = BaseV2DevelopmentAgent._record_execution_bookkeeping(
            task_id="t1",
            result=result,
            exec_result=exec_result,
            repo_path=tmp_path,
            feature_branch_name="feature/t1",
            git_agent=git_agent,
            logger=self._logger(),
        )

        assert completed_count == 2
        assert failed_count == 1
        assert result.iterations_used == 2
        git_agent.commit_current_changes.assert_called_once_with(
            tmp_path, "feat: 2 microtasks completed"
        )

    def test_skips_commit_without_branch_or_method(self, tmp_path: Path):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        result = _FakeWorkflowResult()
        exec_result = _FakeExecResult(["completed"])

        completed_count, failed_count = BaseV2DevelopmentAgent._record_execution_bookkeeping(
            task_id="t1",
            result=result,
            exec_result=exec_result,
            repo_path=tmp_path,
            feature_branch_name=None,
            git_agent=MagicMock(),
            logger=self._logger(),
        )
        assert (completed_count, failed_count) == (1, 0)
        assert result.iterations_used == 1

        class _NoCommitAgent:
            pass

        result2 = _FakeWorkflowResult()
        BaseV2DevelopmentAgent._record_execution_bookkeeping(
            task_id="t1",
            result=result2,
            exec_result=exec_result,
            repo_path=tmp_path,
            feature_branch_name="feature/t1",
            git_agent=_NoCommitAgent(),
            logger=self._logger(),
        )
        assert result2.iterations_used == 1

    def test_commit_exception_is_logged_and_swallowed(self, tmp_path: Path, caplog):
        from software_engineering_team.shared.v2_orchestrator import BaseV2DevelopmentAgent

        git_agent = MagicMock()
        git_agent.commit_current_changes.side_effect = RuntimeError("git boom")
        result = _FakeWorkflowResult()
        exec_result = _FakeExecResult(["completed", "review_failed"])

        with caplog.at_level("WARNING", logger="test_record_execution_bookkeeping"):
            completed_count, failed_count = (
                BaseV2DevelopmentAgent._record_execution_bookkeeping(
                    task_id="t1",
                    result=result,
                    exec_result=exec_result,
                    repo_path=tmp_path,
                    feature_branch_name="feature/t1",
                    git_agent=git_agent,
                    logger=self._logger(),
                )
            )

        assert (completed_count, failed_count) == (1, 1)
        assert result.iterations_used == 1
        assert any(
            "Git agent commit_current_changes raised" in r.message for r in caplog.records
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py::TestRecordExecutionBookkeeping \
  -v
```

Expected: FAIL with `AttributeError: type object 'BaseV2DevelopmentAgent' has no attribute '_record_execution_bookkeeping'`.

- [ ] **Step 3: Implement the helper**

In `shared/v2_orchestrator.py`:

1. Update the module docstring so it also lists post-execution bookkeeping (`_record_execution_bookkeeping`) among the shared members (alongside planning + feature-branch setup).

2. Add this method on `BaseV2DevelopmentAgent` immediately after `_run_planning_and_branch_setup` (before `_read_repo_code`):

```python
    @staticmethod
    def _record_execution_bookkeeping(
        *,
        task_id: str,
        result: Any,
        exec_result: Any,
        repo_path: Path,
        feature_branch_name: Optional[str],
        git_agent: Any,
        logger: logging.Logger,
    ) -> Tuple[int, int]:
        """Count execution outcomes, set ``iterations_used``, and commit mid-workflow.

        Extracted from ``run_workflow`` so completed/failed counting,
        ``result.iterations_used``, and the optional
        ``commit_current_changes`` call are defined once. Status values are
        compared as strings (``\"completed\"`` / ``\"review_failed\"``) so this
        base never imports a team-specific ``MicrotaskStatus`` enum; both
        teams' enums use those values today. ``git_agent`` is pre-resolved by
        the caller since ``ToolAgentKind`` differs per team.

        Preconditions: ``exec_result.microtasks`` is iterable; each item has a
          ``status`` comparable to ``\"completed\"`` / ``\"review_failed\"``.
          ``result`` has a writable ``iterations_used`` attribute.
        Postconditions: ``result.iterations_used`` equals the completed count.
          Returns ``(completed_count, failed_count)``. When
          ``feature_branch_name`` is truthy and ``git_agent`` exposes
          ``commit_current_changes``, that method is invoked once with
          ``repo_path`` and a ``feat: {N} microtasks completed`` message;
          exceptions from the commit are logged as warnings and never raised.
          Never raises on its own.
        """
        completed_count = sum(
            1 for mt in exec_result.microtasks if mt.status == "completed"
        )
        failed_count = sum(
            1 for mt in exec_result.microtasks if mt.status == "review_failed"
        )
        result.iterations_used = completed_count

        if (
            feature_branch_name
            and git_agent is not None
            and hasattr(git_agent, "commit_current_changes")
        ):
            try:
                git_agent.commit_current_changes(
                    repo_path, f"feat: {completed_count} microtasks completed"
                )
            except Exception as exc:
                logger.warning(
                    "[%s] Git agent commit_current_changes raised: %s", task_id, exc
                )

        return completed_count, failed_count
```

- [ ] **Step 4: Run helper tests to verify they pass**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py \
  -v
```

Expected: all PASS (including the three new tests).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/shared/v2_orchestrator.py \
  backend/agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py
git commit -m "$(cat <<'EOF'
Extract post-execution bookkeeping onto BaseV2DevelopmentAgent.

EOF
)"
```

---

### Task 2: Wire both team orchestrators

**Files:**
- Modify: `backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py`
- Modify: `backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py`

**Interfaces:**
- Consumes: `BaseV2DevelopmentAgent._record_execution_bookkeeping` from Task 1
- Produces: both `run_workflow` methods call the helper and keep using returned counts for deliver/logging

- [ ] **Step 1: Replace the backend bookkeeping block**

In `backend_code_v2_team/orchestrator.py`, remove `MicrotaskStatus` from the `.models` import list (it is only used by this block).

Replace the block from `completed_count = sum(` through the commit `try`/`except` (ending just before `result.final_files = current_files`) with:

```python
        completed_count, failed_count = self._record_execution_bookkeeping(
            task_id=task_id,
            result=result,
            exec_result=exec_result,
            repo_path=repo_path,
            feature_branch_name=feature_branch_name,
            git_agent=git_agent,
            logger=logger,
        )
```

Leave `result.final_files = current_files` and everything after it unchanged.

- [ ] **Step 2: Replace the frontend bookkeeping block**

In `frontend_code_v2_team/orchestrator.py`, apply the same import removal and the same replacement block (identical call). Leave `result.final_files = current_files` and everything after it unchanged.

- [ ] **Step 3: Run regression tests**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py \
  agents/software_engineering_team/tests/test_backend_code_v2_team.py \
  agents/software_engineering_team/tests/test_frontend_code_v2_team.py \
  -v
```

Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add \
  backend/agents/software_engineering_team/backend_code_v2_team/orchestrator.py \
  backend/agents/software_engineering_team/frontend_code_v2_team/orchestrator.py
git commit -m "$(cat <<'EOF'
Wire backend and frontend run_workflow through shared bookkeeping helper.

EOF
)"
```

---

### Task 3: Lint and full SE verification

**Files:**
- None expected (verification only)

- [ ] **Step 1: Lint**

```bash
cd backend && make lint
```

Expected: PASS (no new ruff findings on touched files).

- [ ] **Step 2: Run targeted coverage on touched modules**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_v2_orchestrator_helpers.py \
  agents/software_engineering_team/tests/test_backend_code_v2_team.py \
  agents/software_engineering_team/tests/test_frontend_code_v2_team.py \
  --cov=software_engineering_team.shared.v2_orchestrator \
  --cov=software_engineering_team.backend_code_v2_team.orchestrator \
  --cov=software_engineering_team.frontend_code_v2_team.orchestrator \
  --cov-report=term-missing \
  --cov-fail-under=90
```

Expected: coverage ≥90% for the reported packages/modules; tests PASS.

If orchestrator modules stay under 90% because large `run_workflow` paths are `# pragma: no cover` integration code, document that in the PR body and do not add `pragma` without justification — prefer confirming the new helper itself is fully covered via `test_v2_orchestrator_helpers.py`.

- [ ] **Step 3: Optional broader check**

```bash
cd backend && make test
```

Expected: PASS (or investigate any failures unrelated to this extract before merging).

- [ ] **Step 4: No commit unless Step 1–2 required a fix**

If a lint/coverage fix was needed, commit it:

```bash
git add -u
git commit -m "$(cat <<'EOF'
Fix lint/coverage after bookkeeping helper extract.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| Shared helper on `BaseV2DevelopmentAgent` | Task 1 |
| Counts + `iterations_used` + commit | Task 1 |
| String status comparison | Task 1 |
| Both orchestrators call helper | Task 2 |
| `final_files` stays in callers | Task 2 |
| Helper unit tests | Task 1 |
| Existing team tests unchanged / pass | Task 2–3 |
| `make lint` / ≥90% coverage | Task 3 |
| Out of scope (other run_workflow slices) | Not tasked |
