# Fix vacuous Temporal package `os.getenv` import test

**Date:** 2026-08-06  
**Complexity:** 2

## Problem

`test_temporal_package_init_does_not_call_os_getenv` in
`backend/agents/investment_team/tests/test_temporal_bootstrap.py` is meant to
guard against `os.getenv` during `investment_team.temporal.__init__` execution
(the temporalio sandbox replays package `__init__` at workflow registration).

The current sequence is vacuous:

1. Purge `investment_team.temporal*` from `sys.modules`.
2. Install an `os.getenv` spy.
3. Import `investment_team.temporal.workflows` — this runs package `__init__`
   while the spy is active.
4. `spy.reset_mock()` — discards any calls from step 3.
5. Import `investment_team.temporal` again — package is already cached, so
   `__init__` does not re-run.
6. Assert `spy.call_count == 0` — always true regardless of `__init__` behavior.

## Scope

- **In scope:** Restructure that one test so the spy is active during a real
  (re-)execution of `investment_team.temporal.__init__`.
- **Out of scope:** Production `__init__` changes; auditing other bootstrap
  tests; separately covering submodule `os.getenv` usage.

## Design

Match the sibling test `test_importing_temporal_package_does_not_call_start_team_worker`
and the issue-suggested fix:

1. `_purge("investment_team.temporal")` (existing helper already removes the
   package and all `prefix.` submodules).
2. `with mock.patch.object(os, "getenv", wraps=os.getenv) as spy:`
3. `importlib.import_module("investment_team.temporal")` — first import under
   the spy; forces `__init__` to execute.
4. Assert `spy.call_count == 0` with the existing error message.
5. Do **not** call `spy.reset_mock()`.
6. Do **not** pre-import `workflows` before the assertion.

No production code changes. Current compliant `__init__` (no module-level
`os.getenv`) must keep the test green.

## Acceptance Criteria

- Test purges `investment_team.temporal` (and submodules via `_purge`) before
  installing the spy.
- Spy is active during actual (re-)execution of package `__init__`, not reset
  before the assertion.
- Assertion exercises the package `__init__` path (not a cached no-op import).
- Test fails if `os.getenv` is introduced in `investment_team.temporal.__init__`.
- Existing passing behavior for the compliant `__init__` is preserved.

## Testing

```bash
pytest agents/investment_team/tests/test_temporal_bootstrap.py::test_temporal_package_init_does_not_call_os_getenv -q
```

Optional mutation check (do not commit): temporarily add `os.getenv("X")` to
`temporal/__init__.py` and confirm the test fails, then revert.
