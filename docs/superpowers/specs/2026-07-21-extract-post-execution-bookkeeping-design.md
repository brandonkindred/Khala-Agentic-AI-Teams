# Extract post-execution bookkeeping into BaseV2DevelopmentAgent

**Date:** 2026-07-21  
**Status:** Approved for implementation planning

## Goal

Collapse the identical post-execution bookkeeping block in
`BackendDevelopmentAgent.run_workflow` and `FrontendDevelopmentAgent.run_workflow`
into one shared helper on `BaseV2DevelopmentAgent`, so counts, `iterations_used`,
and the mid-workflow `commit_current_changes` call live in a single place.

## Motivation

Both teams compute `completed_count` / `failed_count`, set
`result.iterations_used`, and optionally call `git_agent.commit_current_changes`
with the same logic after execution produces files. This is one of the
mechanical slices of the backend/frontend orchestrator convergence work. A prior
sibling already moved planning + branch creation onto the base
(`_run_planning_and_branch_setup`); this change follows that pattern for the
post-execution slice only.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Approach | Shared static helper on `BaseV2DevelopmentAgent` (approach A) |
| Method name | `_record_execution_bookkeeping` |
| Status comparison | Compare microtask status by string value (`"completed"` / `"review_failed"`) so the base never imports team-specific `MicrotaskStatus` enums |
| Return value | `(completed_count, failed_count)` — callers still need both for deliver success / final logging |
| Commit behavior | Preserve today's guard (`feature_branch_name` and `git_agent` with `commit_current_changes`) and swallow/log exceptions as warnings |
| `result.final_files` | Stays in callers (not part of this block) |
| Existing team tests | Expected to pass unchanged |

## Architecture

### Helper contract

`BaseV2DevelopmentAgent._record_execution_bookkeeping` (static method) in
`shared/v2_orchestrator.py`:

**Preconditions:**
- `exec_result` exposes an iterable `microtasks` whose items have a `status`
  comparable to the strings `"completed"` and `"review_failed"` (both teams'
  enums use those values today).
- `result` is a mutable object with an `iterations_used` attribute.
- `repo_path`, `task_id`, and `logger` are provided by the caller.

**Behavior:**
1. Count microtasks with status value `"completed"` → `completed_count`.
2. Count microtasks with status value `"review_failed"` → `failed_count`.
3. Set `result.iterations_used = completed_count`.
4. If `feature_branch_name` is truthy, `git_agent` is not `None`, and
   `hasattr(git_agent, "commit_current_changes")`, call
   `git_agent.commit_current_changes(repo_path, f"feat: {completed_count} microtasks completed")`
   inside try/except; on exception, `logger.warning` with the same message
   shape as today.
5. Return `(completed_count, failed_count)`.

**Postconditions:**
- `result.iterations_used` equals the completed count.
- Commit is attempted exactly when today's guards would allow it; commit
  failures never raise out of the helper.
- Return tuple is suitable for the existing deliver / logging uses of both
  counts.

### Caller wiring

Both `backend_code_v2_team/orchestrator.py` and
`frontend_code_v2_team/orchestrator.py` replace the duplicated ~20-line block
(after the `if not current_files` early return, before documentation) with:

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

`result.final_files = current_files` and all documentation / deliver / logging
code remain in the team orchestrators.

### Files touched

| Path | Change |
|---|---|
| `shared/v2_orchestrator.py` | Add `_record_execution_bookkeeping`; mention it in the module docstring |
| `backend_code_v2_team/orchestrator.py` | Call the helper |
| `frontend_code_v2_team/orchestrator.py` | Call the helper |
| `tests/test_v2_orchestrator_helpers.py` | Unit tests for the helper (happy path, skip commit, commit exception) |

### Files not touched

- Team `models.py` / `MicrotaskStatus` enums
- Documentation / deliver phases
- `test_backend_code_v2_team.py` / `test_frontend_code_v2_team.py` (expected pass unchanged)

## Error handling

Identical to today: commit exceptions are logged and ignored; counting never
raises on its own. Invalid/missing `microtasks` is a caller bug (precondition).

## Testing

- New unit tests in `test_v2_orchestrator_helpers.py` mirroring
  `TestRunPlanningAndBranchSetup` style (injected fake `exec_result` /
  `git_agent` / `result`).
- Cases: sets counts + `iterations_used` and commits when guards pass; skips
  commit when branch missing or git agent lacks the method; commit exception
  is logged and counts still returned.
- Regression: `test_v2_orchestrator_helpers.py`, `test_backend_code_v2_team.py`,
  `test_frontend_code_v2_team.py` pass; `make test` / `make lint` from
  `backend/`; 90% coverage floor on touched files.

## Out of scope

- Any other `run_workflow` diff slice (documentation, deliver, final status
  copy, preflight, etc.).
- Unifying the team-specific `MicrotaskStatus` enums.
- Moving `result.final_files = current_files` into the helper.
