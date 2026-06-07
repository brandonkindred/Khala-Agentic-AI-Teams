# shared_concurrency

Small, dependency-light concurrency primitives shared across agent teams.
Importing this package pulls in only the Python standard library.

## `BackgroundHeartbeat`

One driver for the "daemon thread runs a callable on an interval until stopped"
pattern that several teams had independently hand-rolled (a Temporal
`activity.heartbeat()` keep-alive, the founder spec-generation job heartbeat, the
SE job-store heartbeat thread, the blogging pipeline heartbeat, the blogging
event-bus reaper, and the job-service stale-job monitor). The driver is generic —
it knows nothing about Temporal or the job service; it just calls a `beat`
callable every `interval_s` until stopped.

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
