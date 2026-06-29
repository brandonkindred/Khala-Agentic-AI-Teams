# shared_concurrency

Small, dependency-light concurrency primitives shared across agent teams.
Importing this package pulls in only the Python standard library.

## `BackgroundHeartbeat`

One driver for the "daemon thread runs a callable on an interval until stopped"
pattern that several teams had independently hand-rolled (a Temporal
`activity.heartbeat()` keep-alive, the founder spec-generation job heartbeat, the
SE job-store heartbeat thread, the SE and job-service stale-job monitors, the
blogging pipeline heartbeat, and the blogging event-bus reaper). The driver is
generic — it knows nothing about Temporal or the job service; it just calls a
`beat` callable every `interval_s` until stopped.

```python
from shared_concurrency import BackgroundHeartbeat

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

# Fire-and-forget with a caller-held stop handle, beating immediately on start:
stop = threading.Event()
BackgroundHeartbeat(sweep, 60.0, beat_first=True, stop_event=stop).start()
# ... later, from anywhere: stop.set()
```

Parameters cover the axes the original copies differed on:

- `should_continue` — optional predicate checked each tick; returning `False`
  exits the thread on its own (no external stop needed).
- `beat_first` — run one beat before the first wait (e.g. a stale-job sweep that
  should fire immediately on startup) instead of the default wait-then-beat.
- `copy_context` — snapshot `contextvars.copy_context()` and run **both** the beat
  and `should_continue` inside it (so a Temporal activity handle is visible in the
  beater thread). The two run in the same context — there is no asymmetry.
- `on_error` — invoked on any beat/predicate exception; default swallows. A
  raising beat or predicate never kills the loop.
- `stop_event` — inject a caller-owned `threading.Event` so the caller keeps a raw
  stop handle (an injected event is never cleared on `start()`).
- `join_timeout` — bound on `stop()`'s join.

`start()` is idempotent; `is_alive()` reports whether the beater thread is
running; `stop()` is safe to call before `start()` or twice.

### Single liveness owner (coding-team activity)

`software_engineering_team/temporal/activities.py::execute_coding_team_activity`
relies on the background beater as the *sole* liveness mechanism for the whole
run; the orchestrator's update callback only persists progress and does **not**
also heartbeat. This keeps "who keeps the activity alive" unambiguous.

## `parallel_map`

One driver for the "fan a per-item function across a bounded `ThreadPoolExecutor`"
pattern that several teams had each hand-rolled with subtly different ordering,
error, and **context-propagation** semantics. The decisive correctness issue is
the last one: a raw `ThreadPoolExecutor` does **not** copy contextvars into its
workers, so every fan-out site has to remember to wrap submissions in
`contextvars.copy_context().run(...)` or it silently drops the LLM attribution /
request-id contextvars (see `llm_service.attribution`). Routing all fan-out
through this helper fixes worker bounds, exception propagation, and context
propagation once — and new sites get context propagation for free.

```python
from shared_concurrency import parallel_map

# Common case — bounded, order-preserving, context propagated, None skipped:
results = parallel_map(prospects, run_one, max_workers=8)

# Each task gets its own copy_context(), so this propagates by default. Opt out
# only for CPU-only work that explicitly wants no propagation:
sums = parallel_map(rows, crunch, max_workers=4, propagate_context=False)

# Completion order + a failure hook (e.g. flip an "abandoned" progress flag
# before pending tasks are cancelled):
outcomes = parallel_map(
    chunks, review_one, max_workers=4, preserve_order=False,
    skip_none=False, on_first_exception=mark_abandoned,
)
```

Parameters cover the axes the original copies differed on:

- `max_workers` — the pool is sized at `min(max_workers, len(items))`, so a small
  batch never spins up idle threads. Empty input short-circuits to `[]`.
- `preserve_order` — return results aligned to input order (default) or in
  completion order.
- `skip_none` — filter `None` results out (the "return `None` to skip this item"
  convention, default) or keep them positionally.
- `propagate_context` — run each task inside a fresh `contextvars.copy_context()`
  (default) so the caller's attribution/request-id reach the worker.
- `on_first_exception` — optional zero-arg callback fired exactly once, on the
  first worker exception, **before** pending tasks are cancelled and the
  exception re-raises.

Error policy is a single, documented **fast-fail**: the first worker exception is
observed in completion order (never delayed behind a slower earlier task),
pending tasks are cancelled (`cancel_futures=True`), and the exception propagates
with its original traceback while already-running tasks finish in the background.

Migrated callers: the sales pod's per-prospect / decision-maker / dossier
fan-outs (`sales_team/orchestrator.py`), the blog research agent's document
scoring and summarization (`blogging/blog_research_agent/agent.py`), and the SE
code-review coordinator's per-chunk map (`code_review_agent/coordinator.py`).
