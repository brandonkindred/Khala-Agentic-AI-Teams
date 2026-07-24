# Design: Add DbC docstring to `_run_backtest_background`

**Issue:** #2224  
**Branch / worktree:** `docs/2224-run-backtest-background-docstring`  
**Date:** 2026-07-24

## Problem

`_run_backtest_background` in `backend/agents/investment_team/api/main.py` has
no docstring. Callers and maintainers lack Design-by-Contract documentation for
job-store preconditions, status transitions, cancel early-returns, and the
`_backtests` persist side effect. Sibling `_run_paper_trading_background` in the
same file already documents Preconditions/Postconditions in this style.

## Goal

Add a docstring with Preconditions/Postconditions that mirrors
`_run_paper_trading_background`, covering `job_id`, `strategy`, `config`,
`submitted_by`, `notes`, status transitions, cancel preservation, and success-path
persistence.

## Non-goals

- No production logic changes.
- No edits to `_run_paper_trading_background` or other helpers.
- No new tests (documentation-only change).

## Design

### File touched

Only `backend/agents/investment_team/api/main.py`, on `_run_backtest_background`.

### Docstring to add

```python
"""Background worker: run a real-data backtest and persist the completed record.

Long-running (market data + sandbox execution), so this runs off the request
thread (or via Temporal dispatch) to avoid proxy timeouts.

Preconditions:
    - ``job_id`` must already exist in the backtest job store (created by
      ``run_backtest`` / ``_bt_create_job``), typically with status PENDING
    - ``strategy`` must be a valid ``StrategySpec`` suitable for
      ``_run_real_data_backtest``
    - ``config`` must be a valid ``BacktestConfig``
    - ``submitted_by`` and ``notes`` are recorded on the resulting
      ``BacktestRecord`` as-is

Postconditions:
    - On the success path: job status becomes RUNNING then COMPLETED with a
      serialized ``RunBacktestResponse``; a new ``BacktestRecord`` is stored
      under ``_backtests[backtest_id]``
    - On ``HTTPException`` or other exceptions: job status becomes FAILED with
      an error string (unless cancelled — see below)
    - If ``_bt_is_job_cancelled(job_id)`` is true at any check point, return
      without writing COMPLETED or FAILED so the cancelled status is preserved
"""
```

### Why these postconditions

| Path | Body behavior |
|---|---|
| Success | `_bt_update_job(..., RUNNING)` → backtest → store `_backtests[backtest_id]` → `_bt_update_job(..., COMPLETED, result=...)` |
| `HTTPException` / other | `_bt_update_job(..., FAILED, error=...)` unless cancelled |
| Cancelled at any check | early `return`; no COMPLETED/FAILED write |

## Testing

No automated test for docstring text. After the edit, re-read the function body
and confirm every status path and cancel early-return appears in Postconditions.

## Success criteria

1. `_run_backtest_background` has Preconditions/Postconditions as above.
2. Cancel early-return is documented (no COMPLETED/FAILED overwrite).
3. Function body unchanged.
4. Only `backend/agents/investment_team/api/main.py` changes for the fix itself
   (plus design/plan docs under `docs/superpowers/`).
