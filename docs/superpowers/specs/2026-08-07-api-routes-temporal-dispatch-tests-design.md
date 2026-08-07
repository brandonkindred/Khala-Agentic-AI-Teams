# Design: Drop redundant is_temporal_enabled patches from product-analysis API route tests

Date: 2026-08-07

## Goal

Align `test_api_product_analysis_routes.py` with Temporal-only dispatch by
removing redundant `is_temporal_enabled=False` “even when disabled” cases,
while leaving already-migrated `test_api_more_routes.py` untouched.

## Context

Parent work: SE Temporal-mandatory / remove test-only Temporal-disable
guards. Product-analysis run / start-from-spec routes already call
`start_standalone_workflow` unconditionally.

Current state on `main`:

- `test_api_more_routes.py` already mocks `start_retry_failed_workflow` /
  `start_run_team_workflow` for happy paths (no `is_temporal_enabled`).
  No further change required for this issue.
- `test_api_product_analysis_routes.py` already has an autouse stub of
  `start_standalone_workflow` and a 503-on-dispatch-failure test
  (`test_start_from_spec_keeps_project_dir_on_dispatch_failure`).
- Two `*_even_when_disabled` tests still patch
  `software_engineering_team.temporal.client.is_temporal_enabled` to
  `False` while also mocking `start_standalone_workflow` — redundant once
  the gate patch is meaningless.

## Decisions

| Topic | Choice |
|---|---|
| `test_api_more_routes.py` | Leave untouched (already compliant) |
| Redundant “even when disabled” product-analysis tests | Delete both (option A) |
| Fold dispatch asserts into happy-path tests | No (minimal) |
| Add new 503 tests to `more_routes` | Out of scope |
| Sibling API test files (backend/frontend code-v2, etc.) | Out of scope |

## Change

File: `backend/agents/software_engineering_team/tests/test_api_product_analysis_routes.py`

1. Delete entirely:
   - `test_run_product_analysis_dispatches_to_temporal_even_when_disabled`
   - `test_start_from_spec_dispatches_to_temporal_even_when_disabled`
2. Leave unchanged:
   - Autouse `_stub_background_workflow`
   - Happy-path / validation / answers / auto-answer tests
   - `test_start_from_spec_keeps_project_dir_on_dispatch_failure` (preserves 503 coverage)
   - Module docstring / fixtures / imports except what becomes unused after the delete

File: `backend/agents/software_engineering_team/tests/test_api_more_routes.py`

- No edits.

## Verification

- Grep `test_api_product_analysis_routes.py` for `is_temporal_enabled` → no hits.
- Grep that file for `even_when_disabled` → no hits.
- Run:
  `pytest agents/software_engineering_team/tests/test_api_product_analysis_routes.py -v`
  → all remaining tests pass.
- Confirm `test_start_from_spec_keeps_project_dir_on_dispatch_failure` still present and asserts 503.

## Out of scope

- `test_api_more_routes.py` content changes (already uses `start_*_workflow` mocks).
- Adding 503-on-dispatch-failure coverage to `more_routes`.
- Other test files (sibling sub-issues).
- Production route or worker changes.
