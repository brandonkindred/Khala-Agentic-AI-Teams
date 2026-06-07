# shared_temporal

Single source of truth for Temporal-backed, resumable job execution across
every agent team. Replaces the per-team `temporal/client.py`,
`temporal/worker.py`, and ad-hoc pause/resume logic.

## Migration recipe (per team)

1. **Define workflow + activity.** Create `{team}/temporal/workflows.py`
   with a `@workflow.defn` class whose `run()` simply invokes one
   `@activity.defn` wrapping the team's existing orchestrator entrypoint:

   ```python
   from temporalio import workflow, activity

   @activity.defn
   def run_pipeline(request: dict) -> dict:
       from my_team.orchestrator import run
       return run(request)

   @workflow.defn
   class MyTeamWorkflow:
       @workflow.run
       async def run(self, request: dict) -> dict:
           return await workflow.execute_activity(
               run_pipeline, request, start_to_close_timeout=timedelta(hours=2)
           )
   ```

2. **Mount the standard router** in `{team}/api/main.py`:

   ```python
   from team_contract.job_router import create_job_router
   app.include_router(create_job_router("my_team"), prefix="/api/my-team")
   ```

   This gives you `POST /jobs`, `GET /jobs`, `GET /jobs/{id}`,
   `DELETE /jobs/{id}`, and `POST /jobs/{id}/resume` for free.

3. **Start the worker** during app lifespan:

   ```python
   from shared_temporal import start_team_worker
   from my_team.temporal.workflows import MyTeamWorkflow, run_pipeline

   start_team_worker("my_team", [MyTeamWorkflow], [run_pipeline])
   ```

4. **Dispatch jobs** from your HTTP handlers via `run_team_job`:

   ```python
   from shared_temporal import run_team_job
   from my_team.temporal.workflows import MyTeamWorkflow

   run_team_job(
       team="my_team",
       job_id=job_id,
       workflow=MyTeamWorkflow.run,
       workflow_args=[request.dict()],
   )
   ```

## Checkpoints and human-in-the-loop

Use `save_checkpoint` / `load_checkpoint` at phase boundaries inside an
activity so a retried workflow can skip completed phases. For pauses that
need user input, use `wait_for_input` (thread mode) or a Temporal signal
handler that calls `submit_input` (Temporal mode); both operate on the same
job record fields (`waiting_for`, `inputs`) so the HTTP resume route works
for either mode.

## Background heartbeats (`BackgroundHeartbeat`)

`heartbeat.py` provides one shared driver for the "daemon thread beats a
callable on an interval" pattern that several teams had hand-rolled (a Temporal
`activity.heartbeat()` keep-alive, the founder spec-generation job heartbeat, and
the SE job-store heartbeat thread). The driver is generic — it knows nothing
about Temporal or the job service; it just calls a `beat` callable every
`interval_s` until stopped. Importing it does **not** pull in the Temporal SDK
(every `temporalio` import in this package is function-deferred).

```python
from shared_temporal import BackgroundHeartbeat

# Externally controlled (context manager owns start + stop):
with BackgroundHeartbeat(activity.heartbeat, 30.0, copy_context=True):
    do_long_blocking_work()

# Fire-and-forget, self-terminating via a predicate:
BackgroundHeartbeat(
    lambda: client.heartbeat(job_id),
    120.0,
    should_continue=lambda: job_is_active(job_id),
    on_error=lambda exc: logger.warning("hb %s: %s", job_id, exc),
).start()
```

Parameters cover the four axes the original copies differed on:

- `should_continue` — optional predicate checked each tick; returning `False`
  exits the thread on its own (no external stop needed).
- `copy_context` — snapshot `contextvars.copy_context()` and run the beat inside
  it (so a Temporal activity handle is visible in the beater thread).
- `on_error` — invoked on any beat/predicate exception; default swallows. A
  raising beat or predicate never kills the loop.
- `join_timeout` — bound on `stop()`'s join.

**Single liveness owner (coding-team activity).** `execute_coding_team_activity`
relies on the background beater as the *sole* liveness mechanism for the whole
run; the orchestrator's update callback only persists progress and does **not**
also heartbeat. This keeps "who keeps the activity alive" unambiguous.

## Environment

- `TEMPORAL_ADDRESS` — required; Temporal is mandatory for all teams.
- `TEMPORAL_NAMESPACE` — default `default`.
- `TEMPORAL_TASK_QUEUE` — default `khala`.
- `CODING_TEAM_HEARTBEAT_INTERVAL_S` — interval (seconds) for the coding-team
  activity's background beater; blank/garbage/non-positive falls back to `30`.

## See also

- **`backend/agents/shared_postgres/`** — sibling module that applies the
  same registry idea to Postgres DDL. Each team exports a `SCHEMA:
  TeamSchema` from `<team>/postgres/__init__.py` and its FastAPI lifespan
  calls `register_team_schemas(SCHEMA)` at startup. Unlike `shared_temporal`'s
  Pattern A (import-time side effect), `shared_postgres` uses Pattern B
  (explicit lifespan call) because DDL is synchronous blocking I/O.
