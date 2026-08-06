# Strengthen backtest dispatch fallback thread assertions

**Issue:** #5053  
**Date:** 2026-08-06  
**Complexity:** 1

## Problem

`test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure` in
`backend/agents/investment_team/tests/test_temporal_bootstrap.py` only asserts
that `threading.Thread` was constructed once. It does not verify that
`start()` was called, nor that the thread targets `_run_backtest_background`
with the correct job arguments.

If production code constructed a fallback thread but forgot to start it, the
backtest job would remain orphaned at `pending` — the exact regression this
test is meant to catch — and the test would still pass.

## Scope

- **In scope:** Strengthen assertions in that one test.
- **Out of scope:** Production fallback-dispatch logic; other dispatch-fallback tests.

## Design

After the existing `thread_ctor.assert_called_once()` in
`test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure`, add:

1. `thread_ctor.return_value.start.assert_called_once()` — proves the fallback thread was started.
2. Assert `kwargs["target"] is api_main._run_backtest_background`.
3. Assert thread `args` match the intended worker signature:
   - `args[0]` equals `resp.json()["job_id"]`
   - `args[1]` is the strategy fixture used in the test (`strat`)
   - `args[3]` equals `"agent-1"` (`submitted_by` from the request body)

Do not assert `daemon=True` (not required by acceptance criteria).

No production code changes.

## Acceptance Criteria

- Test asserts `thread_ctor.return_value.start.assert_called_once()`.
- Test asserts the thread's `target` kwarg is `api_main._run_backtest_background`.
- Test asserts the correct `job_id` (and strategy / submitted_by) are passed in the thread's `args`.
- Test fails if a future regression creates the fallback thread without starting it.

## Testing

Run:

```bash
pytest agents/investment_team/tests/test_temporal_bootstrap.py::test_backtest_dispatch_falls_back_to_thread_on_dispatch_failure -q
```

Optionally confirm the start assertion would fail by temporarily commenting out
`thread.start()` in `api/main.py` (do not commit that change).
