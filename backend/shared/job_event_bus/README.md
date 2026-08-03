# shared.job_event_bus

A single implementation of the process-local, per-job **event bus** used to
stream a job's progress to SSE clients. Several teams had independently
hand-rolled this pattern (pipeline threads `publish()`; SSE generators
`subscribe()`/`unsubscribe()` and drain a thread-safe `deque`); this module owns
the algorithm so they don't diverge again.

## Model

The state lives in a **`BusState`** (a lock plus two maps), owned by the hosting
team — not a shared singleton. A team module instantiates its own `BusState` and
binds thin `subscribe`/`unsubscribe`/`publish`/`cleanup_job` wrappers over it:

```python
from shared.job_event_bus import BusState, Subscription
from shared.job_event_bus import subscribe as _subscribe   # etc.

_state = BusState()

def subscribe(job_id: str) -> Subscription:
    return _subscribe(_state, job_id)
```

Keeping the state team-local means each team's module can expose its own
module-level config/aliases (e.g. `_subscribers` for tests) and opt into the
reaper independently.

## Event buffer (bounded)

Each `Subscription` buffers its undrained events in a `deque(maxlen=500)`. If an
SSE consumer falls behind by more than 500 events, the **oldest** events are
silently dropped (the deque evicts from the left on overflow) — a slow reader
loses old progress, never the newest. The cap is a fixed module constant today;
tune it in `bus.py` if a team needs deeper buffering.

## Optional reaper

`reap_once(state, *, ttl_seconds, max_jobs, logger=None, label=...)` is a single
eviction pass: it drops subscriptions whose `last_activity` is older than
`ttl_seconds`, then enforces a hard cap of `max_jobs` (oldest-first).

A team that keeps long-lived streams drives it on an interval. Rather than
re-hand-roll the daemon-thread lifecycle, construct a **`ReaperHandle`** over the
`BusState` and call `ensure_started()` from `subscribe` (idempotent, lazy) and
`shutdown()` from the app's `on_shutdown` hook:

```python
from shared.job_event_bus import BusState, ReaperHandle

_state = BusState()
_reaper = ReaperHandle(
    _state,
    ttl_seconds=lambda: _SUB_TTL_SECONDS,   # callables → retuned live each pass
    max_jobs=lambda: _MAX_JOBS_TRACKED,
    interval_seconds=_REAPER_INTERVAL_SECONDS,
    name="<team>-event-bus-reaper",
    label="<team> event-bus",
)
```

A team with short, explicitly cleaned-up streams can simply never start a reaper
and get unbounded-until-`cleanup_job` behaviour.

### Asyncio alternative: `schedule_periodic_reap`

`ReaperHandle` drives `reap_once` from a background OS thread — the right fit
for a team with its own thread budget. A team hosted in-process on a single
asyncio event loop (e.g. mounted directly into a FastAPI app rather than run
as its own service) instead wants the reaper as a plain `asyncio.Task` on that
same loop, with shutdown as ordinary task cancellation:

```python
from shared.job_event_bus import BusState, schedule_periodic_reap, stop_periodic_reap

_state = BusState()
_reap_task = schedule_periodic_reap(
    _state,
    ttl_seconds=lambda: _SUB_TTL_SECONDS,
    max_jobs=lambda: _MAX_JOBS_TRACKED,
    interval_seconds=_REAPER_INTERVAL_SECONDS,
    label="<team> event-bus",
)
# ... at app shutdown:
await stop_periodic_reap(_reap_task)
```

Call `schedule_periodic_reap` once at startup (from inside a running event
loop) and keep the returned task referenced — an unreferenced `asyncio.Task`
can be garbage-collected mid-sleep and silently stop reaping. Call
`stop_periodic_reap` once at shutdown; it cancels the task and awaits it so
none is left dangling past process lifetime.

**Liveness contract:** when reaping is enabled, consumers MUST call
`Subscription.touch()` at least once per `ttl_seconds` while their stream is
alive — publish-side activity alone is not a reliable proxy for a
quiet-but-connected client.

## Multi-worker caveat

State is process-local. Under `uvicorn --workers N` or multiple replicas, events
published on one worker will not reach SSE clients on another. Run single-worker
or use sticky sessions until a cross-process bus (Postgres `LISTEN/NOTIFY` or
`agents/event_bus/`) is adopted.

## Current consumers

- `blogging/shared/job_event_bus.py` — reaper enabled (`BLOGGING_EVENT_BUS_*`).
- `investment_team/api/job_event_bus.py` — reaper enabled (`INVESTMENT_EVENT_BUS_*`).

Both bind their SSE endpoints to `shared.sse` for the streaming generator (see
`shared.sse/`).
