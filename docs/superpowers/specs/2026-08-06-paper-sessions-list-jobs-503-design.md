# Design: Fail-closed paper-session cleanup on `list_jobs` failure

Date: 2026-08-06

## Goal

When deleting a strategy lab record, a job-service failure while listing
paper-trading sessions must not leave the API as a generic 500 *or* silently
skip cleanup. Translate the failure into HTTP 503, leave the lab
record/strategy/backtest intact, and allow a retry to re-attempt cleanup.

## Context

`_delete_paper_sessions_for_lab_record` calls `JobServiceClient.list_jobs()`
with no local error handling. Transport / configuration failures therefore
bubble as uncaught exceptions.

`delete_strategy_lab_record` already runs paper-session cleanup *before* any
in-memory mutation. An existing test
(`test_delete_strategy_lab_record_preserves_record_when_paper_cleanup_fails`)
asserts that if cleanup raises, the lab record stays retryable. Soft-failing
`list_jobs` (log + return `0`) would break that contract by deleting the lab
card while orphaning paper sessions in the job service.

Elsewhere in the same module, list endpoints already treat
`httpx.HTTPError` and `RuntimeError` (unconfigured `JOB_SERVICE_URL`) as
expected job-service environmental failures.

## Decisions

| Topic | Choice |
|---|---|
| Failure policy | Fail closed — do not delete lab state when paper-session listing fails |
| HTTP status | `503 Service Unavailable` (retryable; service down / unconfigured) |
| Caught exceptions | Narrow: `httpx.HTTPError`, `RuntimeError` (match sibling list endpoints) |
| Soft-fail / return `0` | Rejected — orphans paper sessions |
| Where to raise | Inside `_delete_paper_sessions_for_lab_record` after logging, so all callers share the contract |
| Other `list_jobs` sites | Out of scope (`_purge_strategy_lab_job_storage`, etc.) |

## Behavior

1. `_delete_paper_sessions_for_lab_record(lab_record_id)` constructs the
   paper-trading `JobServiceClient` as today.
2. Wrap only the `list_jobs()` call:
   - On success: continue matching/`delete_job` fan-out unchanged.
   - On `httpx.HTTPError` or `RuntimeError`: log a warning with `exc_info=True`,
     then raise `fastapi.HTTPException(status_code=503, detail=...)` describing
     that paper-trading session cleanup could not reach the job service.
3. `delete_strategy_lab_record` needs no reorder: cleanup still runs first;
   the 503 propagates before any in-memory delete.
4. Unrelated exceptions from later `delete_job` calls keep their current
   behavior (still fail before/while mutating as today’s helpers dictate).

### Error detail (suggested)

Something stable and client-readable, e.g.:

> Paper-trading session cleanup is temporarily unavailable; retry later.

Do not leak internal exception strings into `detail`.

## Contract updates

Update the docstring of `_delete_paper_sessions_for_lab_record`:

- **Postconditions:** unchanged for the success path (return count of
  successfully deleted matching jobs).
- **Raises:** document `HTTPException` 503 when `list_jobs` fails with
  `httpx.HTTPError` or `RuntimeError`.

`delete_strategy_lab_record` postconditions already require cleanup-before-
delete and exception propagation; optionally note that job-service listing
failures surface as 503 rather than a bare 500.

## Testing

Add or update tests in
`backend/agents/investment_team/tests/test_api_main_extra.py`:

1. **Unit:** monkeypatch `JobServiceClient` so `list_jobs` raises
   `httpx.ConnectError` (or another `HTTPError`) →
   `_delete_paper_sessions_for_lab_record` raises `HTTPException` with
   status 503; no deletes attempted.
2. **Unit:** same for `RuntimeError` (unconfigured URL case).
3. **Endpoint / integration-style:** call `delete_strategy_lab_record` (or the
   API client) with a stub whose `list_jobs` fails → response/exception is
   503 and `_strategy_lab_records` / `_strategies` / `_backtests` still hold
   the record.
4. Existing happy-path and concurrent-delete tests must remain green.
5. Existing
   `test_delete_strategy_lab_record_preserves_record_when_paper_cleanup_fails`
   remains valid (generic raise still preserves state).

## Out of scope

- Soft-fail / return `0` on `list_jobs` failure
- Retry/backoff or circuit-breaker around the job service
- Changes to `_purge_strategy_lab_job_storage` or other `list_jobs` call sites
- Broader HTTP status taxonomy for all job-service errors

## Acceptance

- `list_jobs` environmental failure → HTTP 503, lab state intact, warning logged
- Successful `list_jobs` path unchanged
- New tests fail before the fix and pass after
- Touched investment-team lint/tests pass
