# shared_sse

A single implementation of the **Server-Sent-Events streaming generator** used by
per-job progress endpoints (`GET /…/{job_id}/stream`). Several teams had cloned
the same generator — emit an initial snapshot, relay events published on the
per-job event bus, send a terminal `done` once a terminal event arrives, keep the
connection alive with comment pings until a deadline — and the copies drifted
(one omitted the reaper-liveness `touch()`). This module owns the one contract.

## API

- `sse_line(data: dict) -> str` — frame a dict as a `data: <json>\n\n` line
  (`json.dumps(..., default=str)`).
- `SSE_KEEPALIVE` — the `": keepalive\n\n"` comment line.
- `sse_job_stream_sync(...) -> Iterator[str]` — sync generator (Starlette runs it
  in a thread); blocks on `sub.notify.wait` between drains, waking immediately on
  publish.
- `sse_job_stream_async(...) -> AsyncIterator[str]` — async generator; `await`s a
  poll sleep between drains, never blocking the event loop.

Both take the team-specific pieces as parameters:

```python
from shared_sse import sse_job_stream_sync, sse_line

return StreamingResponse(
    sse_job_stream_sync(
        subscribe=subscribe,            # bus subscribe(job_id) -> sub handle
        unsubscribe=unsubscribe,        # always called in a finally
        job_id=job_id,
        snapshot=_snapshot_event,       # () -> dict | None  (None skips the snapshot)
        terminal_types=("complete", "error", "cancelled"),
    ),
    media_type="text/event-stream",
)
```

The already-terminal short-circuit (snapshot + `done`, closed immediately) stays
in each endpoint because it needs the team's state lookup; it reuses `sse_line`.

## Bus coupling

The bus is consumed structurally via the `sub` handle returned by `subscribe`
(`sub.events` deque, `sub.touch()`, `sub.notify`), so this module has **no import
dependency** on `shared_job_event_bus`. Both stream functions call `sub.touch()`
every pass so an event-bus reaper does not evict an actively-connected consumer.

## Current consumers

- `blogging/api/main.py` — `sse_job_stream_sync`.
- `investment_team/api/main.py` — `sse_job_stream_async`.
- `deepthought/api/main.py` — `SSE_KEEPALIVE` only (its frames are `event:`-named,
  pre-serialized payloads, not the `data: <dict>` shape).
