"""Unit tests for the shared SSE streaming helpers (DB-free, no team deps)."""

from __future__ import annotations

import asyncio
import threading
from collections import deque

import pytest

from shared.sse import (
    SSE_KEEPALIVE,
    sse_job_stream_async,
    sse_job_stream_sync,
    sse_line,
)


class _FakeSub:
    """Minimal stand-in for a bus subscription (events deque + notify + touch)."""

    def __init__(self) -> None:
        self.events: deque = deque()
        self.notify = threading.Event()
        self.touched = 0
        self.closed = False

    def touch(self) -> None:
        self.touched += 1


def _bus(sub: _FakeSub):
    """Return (subscribe, unsubscribe, calls) wired to *sub*."""
    calls = {"unsubscribed": []}

    def subscribe(job_id: str) -> _FakeSub:
        return sub

    def unsubscribe(job_id: str, s: _FakeSub) -> None:
        calls["unsubscribed"].append((job_id, s))

    return subscribe, unsubscribe, calls


# ---------------------------------------------------------------------------
# Framing helpers
# ---------------------------------------------------------------------------


def test_sse_line_frames_json() -> None:
    assert sse_line({"type": "x", "n": 1}) == 'data: {"type": "x", "n": 1}\n\n'


def test_sse_line_falls_back_to_str_for_nonserializable() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird"

    line = sse_line({"v": Weird()})
    assert line.startswith("data: ") and "weird" in line


def test_keepalive_constant_is_a_comment_line() -> None:
    assert SSE_KEEPALIVE == ": keepalive\n\n"


# ---------------------------------------------------------------------------
# Sync stream
# ---------------------------------------------------------------------------


def test_sync_snapshot_then_drain_then_terminal_done() -> None:
    sub = _FakeSub()
    sub.events.append({"type": "progress", "n": 1})
    sub.events.append({"type": "complete", "ok": True})
    subscribe, unsubscribe, calls = _bus(sub)

    out = list(
        sse_job_stream_sync(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id="j",
            snapshot=lambda: {"type": "snapshot"},
            terminal_types=("complete", "error"),
            poll_interval=0.0,
        )
    )

    assert out[0] == sse_line({"type": "snapshot"})
    assert any('"type": "progress"' in line for line in out)
    assert any('"type": "complete"' in line for line in out)
    assert out[-1] == sse_line({"type": "done"})
    assert sub.touched >= 1
    assert calls["unsubscribed"] == [("j", sub)]


def test_sync_skips_snapshot_when_none() -> None:
    sub = _FakeSub()
    sub.events.append({"type": "complete"})
    subscribe, unsubscribe, _ = _bus(sub)

    out = list(
        sse_job_stream_sync(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id="j",
            snapshot=lambda: None,
            terminal_types=("complete",),
            poll_interval=0.0,
        )
    )
    # No snapshot line; first frame is the event itself.
    assert all("snapshot" not in line for line in out)
    assert '"type": "complete"' in out[0]
    assert out[-1] == sse_line({"type": "done"})


def test_sync_keepalive_then_terminal_across_passes() -> None:
    sub = _FakeSub()
    sub.events.append({"type": "progress", "n": 1})  # non-terminal, drained pass 1
    subscribe, unsubscribe, _ = _bus(sub)

    gen = sse_job_stream_sync(
        subscribe=subscribe,
        unsubscribe=unsubscribe,
        job_id="j",
        snapshot=lambda: None,
        terminal_types=("complete",),
        poll_interval=0.0,
    )

    first = next(gen)  # progress line (non-terminal drain)
    assert '"type": "progress"' in first
    second = next(gen)  # keepalive — queue now empty, no terminal
    assert second == SSE_KEEPALIVE

    # A terminal event arriving on a later pass ends the stream.
    sub.events.append({"type": "complete"})
    assert '"type": "complete"' in next(gen)
    assert next(gen) == sse_line({"type": "done"})
    with pytest.raises(StopIteration):
        next(gen)


def test_sync_closes_when_subscription_detached_by_reaper() -> None:
    # A subscription the bus detaches (sub.closed) must end the stream with a
    # terminal error + done, not ping keepalives until the deadline.
    sub = _FakeSub()
    sub.events.append({"type": "progress", "n": 1})  # buffered before eviction
    subscribe, unsubscribe, calls = _bus(sub)

    gen = sse_job_stream_sync(
        subscribe=subscribe,
        unsubscribe=unsubscribe,
        job_id="j",
        snapshot=lambda: None,
        terminal_types=("complete",),
        poll_interval=0.0,
    )
    first = next(gen)  # the buffered progress event is still delivered
    assert '"type": "progress"' in first

    sub.closed = True  # reaper detaches the subscription
    second = next(gen)  # terminal error frame
    assert '"type": "error"' in second
    assert next(gen) == sse_line({"type": "done"})
    with pytest.raises(StopIteration):
        next(gen)
    assert calls["unsubscribed"] == [("j", sub)]


def test_sync_closed_with_queued_terminal_delivers_terminal_not_error() -> None:
    # Race guard: cleanup_job enqueues the terminal event, THEN marks the sub
    # closed. If the close flag is observed before the terminal is drained, the
    # generator must still re-drain and deliver the real terminal — never a
    # spurious eviction error on the normal completion path.
    sub = _FakeSub()
    subscribe, unsubscribe, _ = _bus(sub)

    gen = sse_job_stream_sync(
        subscribe=subscribe,
        unsubscribe=unsubscribe,
        job_id="j",
        snapshot=lambda: None,
        terminal_types=("complete",),
        poll_interval=0.0,
    )
    assert next(gen) == SSE_KEEPALIVE  # empty, still attached

    # Terminal published, then sub detached+closed (the cleanup_job ordering).
    sub.events.append({"type": "complete", "ok": True})
    sub.closed = True

    out = [next(gen), next(gen)]  # complete frame, then done
    assert '"type": "complete"' in out[0]
    assert out[1] == sse_line({"type": "done"})
    assert all('"type": "error"' not in line for line in out)
    with pytest.raises(StopIteration):
        next(gen)


# ---------------------------------------------------------------------------
# Async stream
# ---------------------------------------------------------------------------


def test_async_snapshot_then_terminal_done() -> None:
    sub = _FakeSub()
    sub.events.append({"type": "complete"})
    subscribe, unsubscribe, calls = _bus(sub)

    async def _collect():
        out = []
        async for line in sse_job_stream_async(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id="run",
            snapshot=lambda: {"type": "snapshot"},
            terminal_types=("complete", "error"),
            poll_interval=0.0,
        ):
            out.append(line)
        return out

    out = asyncio.run(_collect())
    assert out[0] == sse_line({"type": "snapshot"})
    assert out[-1] == sse_line({"type": "done"})
    assert sub.touched >= 1
    assert calls["unsubscribed"] == [("run", sub)]


def test_async_awaitable_snapshot_then_terminal_done() -> None:
    """Async snapshot suppliers must be awaited (sync SnapshotFn still works)."""
    sub = _FakeSub()
    sub.events.append({"type": "complete"})
    subscribe, unsubscribe, calls = _bus(sub)

    async def _async_snapshot():
        return {"type": "snapshot", "via": "awaitable"}

    async def _collect():
        out = []
        async for line in sse_job_stream_async(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id="run",
            snapshot=_async_snapshot,
            terminal_types=("complete", "error"),
            poll_interval=0.0,
        ):
            out.append(line)
        return out

    out = asyncio.run(_collect())
    assert out[0] == sse_line({"type": "snapshot", "via": "awaitable"})
    assert out[-1] == sse_line({"type": "done"})
    assert calls["unsubscribed"] == [("run", sub)]


def test_async_keepalive_then_terminal_across_passes() -> None:
    sub = _FakeSub()
    subscribe, unsubscribe, _ = _bus(sub)

    async def _drive():
        agen = sse_job_stream_async(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id="run",
            snapshot=lambda: None,
            terminal_types=("error",),
            poll_interval=0.0,
        )
        first = await agen.__anext__()  # keepalive (empty queue, no snapshot)
        assert first == SSE_KEEPALIVE
        sub.events.append({"type": "error"})
        assert '"type": "error"' in await agen.__anext__()
        assert await agen.__anext__() == sse_line({"type": "done"})
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()

    asyncio.run(_drive())


def test_async_closes_when_subscription_detached_by_reaper() -> None:
    sub = _FakeSub()
    subscribe, unsubscribe, _ = _bus(sub)

    async def _drive():
        agen = sse_job_stream_async(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id="run",
            snapshot=lambda: None,
            terminal_types=("complete",),
            poll_interval=0.0,
        )
        assert await agen.__anext__() == SSE_KEEPALIVE  # empty, still attached
        sub.closed = True  # reaper detaches the subscription
        assert '"type": "error"' in await agen.__anext__()
        assert await agen.__anext__() == sse_line({"type": "done"})
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()

    asyncio.run(_drive())


def test_async_closed_with_queued_terminal_delivers_terminal_not_error() -> None:
    # Async counterpart of the race guard: a terminal queued before the close
    # flag is observed must still be delivered, not replaced by an eviction error.
    sub = _FakeSub()
    subscribe, unsubscribe, _ = _bus(sub)

    async def _drive():
        agen = sse_job_stream_async(
            subscribe=subscribe,
            unsubscribe=unsubscribe,
            job_id="run",
            snapshot=lambda: None,
            terminal_types=("complete",),
            poll_interval=0.0,
        )
        assert await agen.__anext__() == SSE_KEEPALIVE
        sub.events.append({"type": "complete", "ok": True})
        sub.closed = True
        assert '"type": "complete"' in await agen.__anext__()
        assert await agen.__anext__() == sse_line({"type": "done"})
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()

    asyncio.run(_drive())
