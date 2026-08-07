# Design: Update coding-team run Temporal dispatch tests for unconditional dispatch

Date: 2026-08-07

## Goal

Make `test_coding_team_run_temporal_dispatch.py` reflect Temporal-only
dispatch: no thread-fallback case, and no `is_temporal_enabled` patches.

## Context

Parent work: SE Temporal-mandatory / remove test-only Temporal-disable
guards. Dispatch-removal for coding-team `/run` already landed; the route
calls `start_coding_team_workflow` unconditionally.

Current file state on `main`:

- Module docstring already says unconditional Temporal dispatch.
- `test_run_uses_thread_path_when_temporal_disabled` is already absent.
- `test_run_dispatches_via_temporal_even_when_disabled` still patches
  `shared.temporal.is_temporal_enabled` to `False` — redundant with the
  happy-path test once the patch is meaningless.

## Decisions

| Topic | Choice |
|---|---|
| Redundant “even when disabled” test | Delete it (option A) |
| Rename happy-path test | No (approach 1 — minimal) |
| Other SE test files | Out of scope |

## Change

File: `backend/agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py`

1. Delete `test_run_dispatches_via_temporal_even_when_disabled` entirely.
2. Leave unchanged:
   - `test_run_dispatches_via_temporal_when_enabled`
   - `test_run_marks_job_failed_and_503_when_temporal_dispatch_raises`
   - `test_run_without_plan_input_creates_row_and_stays_pending`
   - Module docstring / imports / fixtures

## Verification

- Grep the file for `is_temporal_enabled` → no hits.
- Run:
  `pytest agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py -v`
  → all remaining tests pass (3 cases).

## Out of scope

- Other test files (product-analysis, frontend/backend code-v2 APIs, etc.).
- Production route or worker changes.
- Renaming `test_run_dispatches_via_temporal_when_enabled`.
