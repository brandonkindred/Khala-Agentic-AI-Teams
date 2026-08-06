# Design: Always-return status from `_run_backtest_background`

Date: 2026-08-06

Related: GitHub issue tracking return-type / status-string contract for
`_run_backtest_background` (investment team backtest worker).

## Goal

Make `_run_backtest_background` return a terminal status string on every path
(`completed` / `failed` / `cancelled`), and have `run_backtest_activity`
consume that return value instead of re-reading the job store after the
worker finishes.

## Context

Today `_run_backtest_background` is annotated `-> None` and cancel checkpoints
use bare `return`. Persistence already writes COMPLETED / FAILED via
`_bt_update_job`; cancel paths leave the cancelled job-store status alone.
`run_backtest_activity` ignores the worker return and calls
`_backtest_job_status(job_id)` afterward to decide ApplicationError /
cancelled / completed.

Issue analysis assumed cancel paths already returned
`_BT_JOB_STATUS_CANCELLED` with an `Optional[str]` annotation. On current
`main` they do not. This design implements the stronger always-return
contract and wires the Temporal activity to it.

## Decisions

| Topic | Choice |
|---|---|
| Return type | `-> str` (always a terminal status constant) |
| Status values | Existing `_BT_JOB_STATUS_{COMPLETED,FAILED,CANCELLED}` only |
| Cancel paths | `return _BT_JOB_STATUS_CANCELLED` (no COMPLETED/FAILED write) |
| Success / fail paths | Persist via `_bt_update_job`, then return the matching constant |
| Temporal activity | Use worker return for outcome; keep entry short-circuit via job store |
| Thread dispatch | Unchanged (Thread target discards return) |
| Job store | Remains persistence source of truth; return mirrors what was written / preserved |

## Contract

### `_run_backtest_background`

```python
def _run_backtest_background(
    job_id: str,
    strategy: StrategySpec,
    config: BacktestConfig,
    submitted_by: str,
    notes: List[str],
) -> str:
```

**Preconditions:** unchanged (job exists; strategy/config valid).

**Postconditions:**

- Cancel checkpoint (`_bt_is_job_cancelled` true): return
  `_BT_JOB_STATUS_CANCELLED` without writing COMPLETED or FAILED.
- Success: persist COMPLETED record, return `_BT_JOB_STATUS_COMPLETED`.
- `BacktestExecutionError` / other exception without cancel: persist FAILED,
  return `_BT_JOB_STATUS_FAILED`.
- Exception path with cancel: return `_BT_JOB_STATUS_CANCELLED` (no FAILED
  overwrite).

### `run_backtest_activity`

- Keep: if `_backtest_job_status(job_id) == COMPLETED` at entry → return
  completed dict without recompute.
- Change: `final_status = _run_backtest_background(...)`.
- Branch on `final_status`:
  - FAILED → raise `ApplicationError`
  - CANCELLED → `{"job_id", "status": "cancelled"}`
  - else → `{"job_id", "status": "completed"}`
- Remove the post-call `_backtest_job_status` read used solely for outcome.

## Files

| File | Change |
|---|---|
| `backend/agents/investment_team/api/main.py` | Annotation `-> str`; return constants on all paths; docstring postconditions |
| `backend/agents/investment_team/temporal/workflows.py` | Activity consumes worker return |
| `backend/agents/investment_team/tests/test_api_main_extra.py` | Assert return values on complete / fail / cancel |
| `backend/agents/investment_team/tests/test_temporal_bootstrap.py` | Stubs return status strings; drop second job-status stub for outcome |

## Testing

- Direct worker tests assert returned constant matches path.
- Activity tests: monkeypatched `_run_backtest_background` returns
  COMPLETED / FAILED / CANCELLED; entry short-circuit still uses
  `_backtest_job_status`.
- Existing side-effect assertions (job updates, no duplicate backtest ids)
  remain.

## Out of scope

- Changing thread-mode dispatch to observe the return value
- Broader job-store API redesign
- Paper-trading or other background workers
- Returning non-terminal statuses (e.g. RUNNING)

## Acceptance

- `_run_backtest_background` annotated and implemented as `-> str` with
  COMPLETED / FAILED / CANCELLED on every exit
- `run_backtest_activity` uses that return for outcome branching
- Tests cover worker returns and updated activity stubs
- Lint and investment_team package tests for touched code pass
