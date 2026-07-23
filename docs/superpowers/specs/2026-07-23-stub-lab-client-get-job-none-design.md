# Design: Pin `_StubLabClient.get_job()` missing-ID contract

Date: 2026-07-23

## Goal

Add an explicit unit test that `_StubLabClient.get_job()` returns `None` for a job ID absent from `self.by_id`, matching the real job-service client's missing-job contract so future stub use cannot silently regress into a `KeyError`.

## Context

`_StubLabClient` in `backend/agents/investment_team/tests/test_strategy_lab_routes.py` already implements the correct behavior:

```python
def get_job(self, jid: str) -> Optional[Dict[str, Any]]:
    return dict(self.by_id[jid]) if jid in self.by_id else None
```

Production `JobServiceClient.get_job` returns `None` (via JSON `job` field) when a job is missing. Route handlers treat that as “not found.” Route-level 404 tests already exercise missing runs through handlers, but nothing asserts the stub method’s return value directly. Without that pin, a future edit that drops the membership check would only surface as an unhandled `KeyError` rather than a clear contract failure.

## Decisions

| Topic | Choice |
|---|---|
| Stub implementation | Leave the existing ternary unchanged |
| Coverage style | Direct unit test on `_StubLabClient`, not a new route case |
| Happy path | Also assert a known ID returns a `dict` equal to the seeded job |
| Production client | Out of scope |
| Other stubs | Out of scope |

## Change

In `test_strategy_lab_routes.py`, immediately after the `_StubLabClient` class:

1. `test_stub_lab_client_get_job_returns_none_for_unknown_id` — empty stub; `get_job("missing-id") is None`.
2. `test_stub_lab_client_get_job_returns_copy_for_known_id` — seed one job; `get_job(job_id)` equals that job as a dict (defensive copy semantics of `dict(...)`).

## Acceptance

- Unknown ID → `None` (no `KeyError`).
- Known ID → dict equal to the stored job.
- Existing `_StubLabClient` consumers continue to pass unchanged.
- `LLM_PROVIDER=dummy` — targeted file tests plus `make lint` in `backend/`.

## Out of scope

- Rewriting `get_job` to an explicit `if`/`return None` form.
- Auditing other test doubles for contract drift.
- Changes to `JobServiceClient` or strategy-lab routes.
