# shared_job_event_bus

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
from shared_job_event_bus import BusState, Subscription
from shared_job_event_bus import subscribe as _subscribe   # etc.

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
`ttl_seconds`, then enforces a hard cap of `max_jobs` (oldest-first). A team that
keeps long-lived streams drives it on an interval (e.g. via
`shared_concurrency.BackgroundHeartbeat`); a team with short, explicitly
cleaned-up streams simply never calls it and gets unbounded-until-`cleanup_job`
behaviour.

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
- `investment_team/api/job_event_bus.py` — no reaper (short-lived streams).
