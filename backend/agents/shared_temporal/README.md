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

## Background heartbeats

The "daemon thread beats a callable on an interval" keep-alive that Temporal
activities use (e.g. `execute_coding_team_activity`'s `activity.heartbeat()`
beater) is driven by the shared `BackgroundHeartbeat` helper in the
**`shared_concurrency`** package (it is Temporal-agnostic and also used by
non-Temporal callers). See `backend/agents/shared_concurrency/README.md`.

The coding-team activity's beater interval is set by
`CODING_TEAM_HEARTBEAT_INTERVAL_S` (seconds; blank/garbage/non-positive falls
back to `30`).

## Environment

- `TEMPORAL_ADDRESS` — required; Temporal is mandatory for all teams.
- `TEMPORAL_NAMESPACE` — default `default`.
- `TEMPORAL_TASK_QUEUE` — default `khala`.
- `TEMPORAL_PAYLOAD_COMPRESSION` / `TEMPORAL_PAYLOAD_COMPRESSION_MIN_BYTES` —
  gzip payload codec toggle/threshold (see below).

## Payload compression

`connect_temporal_client` builds its `Client`'s `DataConverter` via
`shared_temporal.codec.build_data_converter`, which installs a gzip
`PayloadCodec` by default. A team whose activities move large, highly
compressible payloads — e.g. `code_review_agent`'s map-reduce chunks, which
carry the full, untruncated diff by design — can otherwise trip Temporal's
512 KiB `PayloadSizeWarning` (`TMPRL1103`) well before hitting any real gRPC
message limit. The codec runs transparently underneath every team's client and
worker (they share this one `connect_temporal_client` call), so no team needs
its own opt-in; see `docs/ENV_VARS.md` for the toggle/threshold env vars and
`shared_temporal/codec.py` for the implementation.

## See also

- **`backend/agents/shared_postgres/`** — sibling module that applies the
  same registry idea to Postgres DDL. Each team exports a `SCHEMA:
  TeamSchema` from `<team>/postgres/__init__.py` and its FastAPI lifespan
  calls `register_team_schemas(SCHEMA)` at startup. Unlike `shared_temporal`'s
  Pattern A (import-time side effect), `shared_postgres` uses Pattern B
  (explicit lifespan call) because DDL is synchronous blocking I/O.
