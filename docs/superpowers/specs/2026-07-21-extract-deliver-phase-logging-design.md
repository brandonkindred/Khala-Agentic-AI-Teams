# Extract deliver-phase + final logging into BaseV2DevelopmentAgent

**Date:** 2026-07-21  
**Status:** Approved for implementation  
**Issue:** #1998 (parent #1982)

## Goal

Collapse the near-identical deliver-phase + final status/logging blocks in
`BackendDevelopmentAgent.run_workflow` and `FrontendDevelopmentAgent.run_workflow`
into one shared helper on `BaseV2DevelopmentAgent`, parameterized by team label
(and the one real in-progress status-text difference).

## Motivation

These blocks differ only in (1) `"Backend"` vs `"Frontend"` final status strings
and (2) whether the in-progress deliver status includes a trailing `"..."`.
Keeping them duplicated is part of the 373-line backend/frontend orchestrator
diff tracked under the parent epic.

## Decisions (locked)

| Decision | Choice |
|---|---|
| Helper name | `_run_deliver_and_finalize` on `BaseV2DevelopmentAgent` |
| Team label | Class attribute `_TEAM_LABEL = "Backend"` / `"Frontend"` |
| In-progress status | Class attribute `_DELIVER_IN_PROGRESS_STATUS` (preserves ellipsis difference) |
| `run_deliver` | Injected callable (same monkeypatch pattern as `_run_planning_and_branch_setup`) |
| `Phase.DELIVER` | Use shared `software_engineering_team.shared.v2_models.Phase` inside the helper |
| Return shape | `Optional[str]` failure reason — caller returns `result` early when non-`None` |
| Observable behavior | Byte-identical status/log strings vs today for both teams |
| Scope | Deliver + finalize block only; no other `run_workflow` convergence |

## Architecture

### Files touched

| Path | Change |
|---|---|
| `shared/v2_orchestrator.py` | Add `_run_deliver_and_finalize`; document `_TEAM_LABEL` / `_DELIVER_IN_PROGRESS_STATUS` |
| `backend_code_v2_team/orchestrator.py` | Set class attrs; replace inline deliver+finalize with helper call |
| `frontend_code_v2_team/orchestrator.py` | Same |
| `tests/test_v2_orchestrator_helpers.py` | Unit tests for the new helper |

### Helper contract

Preconditions:

- `repo_path` is an existing directory
- `run_deliver` matches today's per-team `run_deliver` keyword signature
- `result` is a mutable workflow-result object with the fields the block sets today
- Caller has already set `start_time` via `time.monotonic()` before the helper runs

Postconditions:

- On deliver success: mutates `result` (`deliver_result`, `success`, `summary`,
  optionally `needs_followup`), emits final job update, logs workflow timing,
  returns `None`
- On deliver exception: sets `result.failure_reason`, logs error, returns the
  failure-reason string (caller returns `result` immediately)
- In-progress status text comes from `cls._DELIVER_IN_PROGRESS_STATUS`
- Final status text is `f"{cls._TEAM_LABEL} task complete"` /
  `f"{cls._TEAM_LABEL} task completed with issues"`

### Class attributes

```python
class BackendDevelopmentAgent(BaseV2DevelopmentAgent):
    _TEAM_LABEL = "Backend"
    _DELIVER_IN_PROGRESS_STATUS = "Committing changes and preparing delivery"

class FrontendDevelopmentAgent(BaseV2DevelopmentAgent):
    _TEAM_LABEL = "Frontend"
    _DELIVER_IN_PROGRESS_STATUS = "Committing changes and preparing delivery..."
```

## Out of scope

- Any other `run_workflow` block (execution, documentation, team-lead setup)
- Unifying the ellipsis difference into one string
- Changing `run_deliver` itself

## Acceptance

- Shared helper exists; both agents set class attrs and call it
- `test_v2_orchestrator_helpers.py`, `test_backend_code_v2_team.py`,
  `test_frontend_code_v2_team.py` pass with identical status/log text
- `make test` and `make lint` pass from `backend/`; 90% coverage floor holds
  for touched files
