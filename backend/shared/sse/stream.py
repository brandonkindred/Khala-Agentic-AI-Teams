"""Shared Server-Sent-Events streaming helper for per-job progress endpoints.

Several teams expose ``GET /…/{job_id}/stream`` as SSE: emit an initial snapshot,
relay incremental events published on the per-job event bus, send a terminal
``done`` once a terminal event arrives, and keep the connection alive with comment
pings until a 4-hour deadline. That generator was cloned per endpoint and the
copies drifted (one omitted the reaper-liveness :meth:`touch`). This module owns
the one streaming contract; endpoints supply the team-specific pieces (bus
subscribe/unsubscribe, snapshot lookup, the set of terminal event types).

The bus is consumed structurally via the ``sub`` handle returned by ``subscribe``
(``sub.events`` deque, ``sub.touch()``, ``sub.notify`` event), so this module has
no dependency on :mod:`shared.job_event_bus` itself.

Two entry points preserve each team's concurrency model:

- :func:`sse_job_stream_sync` — a sync generator (Starlette runs it in a thread)
  that blocks on ``sub.notify.wait`` between drains; wakes immediately on publish.
- :func:`sse_job_stream_async` — an async generator that ``await``\\s a poll sleep
  between drains; never blocks the event loop.

Both emit the optional initial snapshot, call ``sub.touch()`` every pass so a
reaper does not evict an actively-connected consumer, and ``unsubscribe`` in a
``finally``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Collection,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

__all__ = [
    "SSE_KEEPALIVE",
    "sse_line",
    "sse_job_stream_sync",
    "sse_job_stream_async",
]

# SSE comment line (ignored by EventSource) that keeps intermediary proxies from
# closing an idle connection.
SSE_KEEPALIVE = ": keepalive\n\n"

# Default terminal types and connection deadline shared by the per-job streams.
_DONE_EVENT: Dict[str, str] = {"type": "done"}
# Sent when the bus detaches the subscription (idle past the TTL, or evicted to
# enforce the job cap) so the client learns the stream ended abnormally and can
# reconnect, instead of receiving keepalives until the deadline. It is an
# ``error`` so a client does not mistake it for successful job completion.
_CLOSED_EVENT: Dict[str, str] = {
    "type": "error",
    "error": "stream closed: the server reclaimed this subscription",
}
_DEFAULT_DEADLINE_SECONDS = 4 * 3600  # 4-hour max connection
_DEFAULT_POLL_INTERVAL = 1.0

# A snapshot supplier returns the initial event payload, or ``None`` to skip it
# (e.g. when the team has no current state to send). The async stream also
# accepts an awaitable supplier so callers can take async locks without
# blocking the event loop.
SnapshotFn = Callable[[], Union[Optional[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]]
SubscribeFn = Callable[[str], Any]
UnsubscribeFn = Callable[[str, Any], None]


def sse_line(data: Dict[str, Any]) -> str:
    """Frame *data* as an SSE ``data:`` line.

    Preconditions:
        - ``data`` is JSON-serializable (non-serializable values fall back to
          ``str`` via ``default=str``).
    Postconditions:
        - Returns ``"data: <json>\\n\\n"`` — one complete SSE event.
    """
    return f"data: {json.dumps(data, default=str)}\n\n"


def _drain_pass(sub: Any, terminal_types: Collection[str]) -> Tuple[List[str], bool]:
    """Drain every queued event on *sub* into framed SSE lines (one pass).

    Preconditions:
        - ``sub.events`` is a deque of event dicts; ``terminal_types`` is the set
          of ``type`` values that end the stream.
    Postconditions:
        - Returns ``(lines, sent_terminal)``. ``lines`` frames each drained event
          in order; when a terminal event was drained, ``sent_terminal`` is True
          and a trailing ``done`` line is appended. The deque is left empty.
    """
    lines: List[str] = []
    sent_terminal = False
    while sub.events:
        event = sub.events.popleft()
        lines.append(sse_line(event))
        if event.get("type") in terminal_types:
            sent_terminal = True
    if sent_terminal:
        lines.append(sse_line(_DONE_EVENT))
    return lines, sent_terminal


def _closed_drain(sub: Any, terminal_types: Collection[str]) -> List[str]:
    """Framed terminal lines for a subscription the bus just detached.

    Preconditions:
        - ``sub.closed`` is True (the bus has detached this subscription).
    Postconditions:
        - Re-drains ``sub.events`` once and returns those framed lines. The bus
          enqueues a job's terminal event *before* marking the subscription
          closed (``publish`` then ``cleanup_job``), and the close flag may be
          observed between the caller's drain and this call, so an unread
          terminal event can still be sitting in the deque — deliver it (with its
          ``done``) when present. Only when no terminal event was drained (a
          reaper eviction, which enqueues nothing) is the synthetic close frame
          (``error`` + ``done``) emitted instead. This makes the close path
          race-free: a normal completion never surfaces as a spurious eviction
          error.
    """
    lines, sent_terminal = _drain_pass(sub, terminal_types)
    if not sent_terminal:
        lines.append(sse_line(_CLOSED_EVENT))
        lines.append(sse_line(_DONE_EVENT))
    return lines


def sse_job_stream_sync(
    *,
    subscribe: SubscribeFn,
    unsubscribe: UnsubscribeFn,
    job_id: str,
    snapshot: SnapshotFn,
    terminal_types: Collection[str],
    deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> Iterator[str]:
    """Sync SSE generator for a per-job event-bus stream.

    Preconditions:
        - ``subscribe(job_id)`` returns a bus subscription handle; the matching
          ``unsubscribe(job_id, sub)`` is always called on exit.
        - ``snapshot()`` returns the initial event payload or ``None`` to skip it.
    Postconditions:
        - Yields the optional snapshot, then framed events as they are published,
          a single ``done`` line after the first terminal event (then stops), and
          a keepalive comment each idle pass until ``deadline_seconds`` elapses.
          ``sub.touch()`` is called every pass to keep a reaper from evicting it.
        - If the bus detaches the subscription anyway (``sub.closed`` — idle past
          the TTL, or evicted to enforce the job cap), yields a terminal error +
          ``done`` and stops, rather than pinging keepalives to the deadline.
    """
    sub = subscribe(job_id)
    try:
        snap = snapshot()
        if snap is not None:
            yield sse_line(snap)

        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            # Liveness signal for the event-bus reaper: this consumer is still
            # reading, so don't evict it even if the job is quiet past the TTL.
            sub.touch()

            lines, sent_terminal = _drain_pass(sub, terminal_types)
            for line in lines:
                yield line
            if sent_terminal:
                return

            # The bus detached this subscription (idle past the TTL, evicted to
            # enforce the job cap, or cleaned up after its terminal event): it
            # will never receive another event, so close the stream now instead
            # of pinging keepalives to the deadline.
            if getattr(sub, "closed", False):
                for line in _closed_drain(sub, terminal_types):
                    yield line
                return

            yield SSE_KEEPALIVE
            # Block until the next publish wakes us, or the poll interval elapses.
            sub.notify.wait(timeout=poll_interval)
            sub.notify.clear()
    finally:
        unsubscribe(job_id, sub)


async def sse_job_stream_async(
    *,
    subscribe: SubscribeFn,
    unsubscribe: UnsubscribeFn,
    job_id: str,
    snapshot: SnapshotFn,
    terminal_types: Collection[str],
    deadline_seconds: float = _DEFAULT_DEADLINE_SECONDS,
    poll_interval: float = _DEFAULT_POLL_INTERVAL,
) -> AsyncIterator[str]:
    """Async SSE generator for a per-job event-bus stream.

    Same contract as :func:`sse_job_stream_sync`, but never blocks the event
    loop: it ``await``\\s a poll sleep between drains instead of blocking on the
    subscription's notify event.

    Preconditions:
        - Same as :func:`sse_job_stream_sync`, except ``snapshot()`` may return
          either a payload/``None`` or an awaitable of the same — awaitables
          are awaited here so async callers can hold ``asyncio.Lock`` while
          building the snapshot without blocking the loop.

    Postconditions:
        - Same framing/lifetime contract as :func:`sse_job_stream_sync`.
    """
    sub = subscribe(job_id)
    try:
        snap = snapshot()
        if inspect.isawaitable(snap):
            snap = await snap
        if snap is not None:
            yield sse_line(snap)

        deadline = time.monotonic() + deadline_seconds
        while time.monotonic() < deadline:
            sub.touch()

            lines, sent_terminal = _drain_pass(sub, terminal_types)
            for line in lines:
                yield line
            if sent_terminal:
                return

            # The bus detached this subscription (idle past the TTL, evicted to
            # enforce the job cap, or cleaned up after its terminal event): close
            # the stream now rather than ping keepalives to the deadline (see the
            # sync variant).
            if getattr(sub, "closed", False):
                for line in _closed_drain(sub, terminal_types):
                    yield line
                return

            yield SSE_KEEPALIVE
            await asyncio.sleep(poll_interval)
    finally:
        unsubscribe(job_id, sub)
