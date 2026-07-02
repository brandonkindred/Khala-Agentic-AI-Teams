# shared_job_store

Neutral, team-agnostic **job-store operations** — the read + human-in-the-loop
(HITL) pause/answer helpers that were byte-identical across the coding team's and
the software-engineering team's `job_store` modules.

## What is shared (and what isn't)

| Shared here | Stays in each team's `job_store` |
|---|---|
| `get_job`, `add_pending_questions`, `submit_answers`, `is_waiting_for_answers`, `get_submitted_answers` | `create_job` / `update_job` / `list_jobs` (team-specific fields, statuses, and list projection), plus each team's extras — SE's `reset_job` / `mark_*` / cancel / heartbeat / task-state, coding's resume-lease + task-graph snapshot. |

## Design: pass the client in

Every function takes the `JobServiceClient` as its first argument rather than
resolving it. Each team's thin wrapper delegates through its own `_client()`:

```python
# software_engineering_team/shared/job_store.py
def get_job(job_id, cache_dir=DEFAULT_CACHE_DIR):
    return shared_job_store.get_job(_client(cache_dir), job_id)
```

This keeps the client fully in the team wrapper's control — the SE test fixture
`patched_job_store` monkeypatches that team's `_client`, and the delegation
naturally routes through the fake because the wrapper resolves the client before
calling the shared function.

## Reconciled drift

`get_submitted_answers` coerces a stored `None` to `[]` (`... or []`), adopting the
coding-team behaviour that strictly dominates SE's prior `.get(..., [])`.
