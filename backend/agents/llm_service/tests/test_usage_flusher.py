"""Unit tests for the batched LLM usage flusher."""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any

import pytest

from llm_service import usage_flusher


class _Rec:
    def __init__(self, **overrides: Any) -> None:
        self.timestamp = datetime.now(tz=timezone.utc).timestamp()
        self.team = "blogging"
        self.agent_key = "writer"
        self.model = "m"
        self.prompt_tokens = 3
        self.completion_tokens = 1
        self.total_tokens = 4
        self.status = "success"
        for k, v in overrides.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _reset():
    usage_flusher._reset_for_test()
    yield
    usage_flusher._reset_for_test()


def test_observer_enqueues_without_db_io(monkeypatch) -> None:
    monkeypatch.setattr(usage_flusher, "is_postgres_enabled", lambda: True)
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("write_rows must not run on the call path")

    monkeypatch.setattr(usage_flusher.usage_store, "write_rows", boom)
    usage_flusher._usage_observer(_Rec())
    assert usage_flusher._buffer_size() == 1
    assert called["n"] == 0


def test_observer_skips_when_postgres_off(monkeypatch) -> None:
    monkeypatch.setattr(usage_flusher, "is_postgres_enabled", lambda: False)
    usage_flusher._usage_observer(_Rec())
    assert usage_flusher._buffer_size() == 0


def test_buffer_cap_drops_oldest(monkeypatch, caplog) -> None:
    monkeypatch.setattr(usage_flusher, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(usage_flusher, "_max_buffer", lambda: 2)
    caplog.set_level("WARNING", logger="llm_service.usage_flusher")
    usage_flusher._usage_observer(_Rec(agent_key="a1"))
    usage_flusher._usage_observer(_Rec(agent_key="a2"))
    usage_flusher._usage_observer(_Rec(agent_key="a3"))
    assert usage_flusher._buffer_size() == 2
    keys = [r[2] for r in usage_flusher._snapshot_buffer()]
    assert keys == ["a2", "a3"]
    assert any("buffer full" in r.message for r in caplog.records)


def test_drain_writes_and_clears(monkeypatch) -> None:
    monkeypatch.setattr(usage_flusher, "is_postgres_enabled", lambda: True)
    written: list = []
    monkeypatch.setattr(
        usage_flusher.usage_store, "write_rows", lambda rows: written.append(rows) or len(rows)
    )
    usage_flusher._usage_observer(_Rec())
    n = usage_flusher.drain()
    assert n == 1
    assert len(written[0]) == 1
    assert usage_flusher._buffer_size() == 0


def test_drain_write_failure_returns_zero(monkeypatch) -> None:
    monkeypatch.setattr(usage_flusher, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(
        usage_flusher.usage_store,
        "write_rows",
        lambda rows: (_ for _ in ()).throw(RuntimeError("db")),
    )
    usage_flusher._usage_observer(_Rec())
    assert usage_flusher.drain() == 0
    assert usage_flusher._buffer_size() == 0


def test_register_idempotent(monkeypatch) -> None:
    registered: list = []
    monkeypatch.setattr(usage_flusher, "_register_call_observer", registered.append)
    monkeypatch.setattr(usage_flusher.BackgroundHeartbeat, "start", lambda self: None)
    usage_flusher.register_usage_flusher()
    usage_flusher.register_usage_flusher()
    assert registered == [usage_flusher._usage_observer]
    assert usage_flusher._is_registered()


def test_shutdown_drains_then_unregisters(monkeypatch) -> None:
    monkeypatch.setattr(usage_flusher, "is_postgres_enabled", lambda: True)
    drained = {"n": 0}
    monkeypatch.setattr(
        usage_flusher, "drain", lambda: drained.__setitem__("n", drained["n"] + 1) or 0
    )
    unregistered: list = []
    monkeypatch.setattr(usage_flusher, "_unregister_call_observer", unregistered.append)
    usage_flusher._mark_registered_for_test()
    usage_flusher.shutdown()
    assert drained["n"] == 1
    assert unregistered == [usage_flusher._usage_observer]
    assert not usage_flusher._is_registered()


def test_drain_empty_buffer_is_zero() -> None:
    assert usage_flusher.drain() == 0


def test_unregister_when_not_registered_is_noop() -> None:
    usage_flusher.unregister()
    assert usage_flusher._is_registered() is False


def test_register_failure_does_not_register(monkeypatch, caplog) -> None:
    caplog.set_level("WARNING", logger="llm_service.usage_flusher")

    def boom(observer):
        raise RuntimeError("llm_service unavailable")

    monkeypatch.setattr(usage_flusher, "_register_call_observer", boom)
    usage_flusher.register_usage_flusher()
    assert usage_flusher._is_registered() is False
    assert usage_flusher._heartbeat is None
    assert any("could not register" in r.message for r in caplog.records)


def test_unregister_stops_heartbeat_and_swallows_observer_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        usage_flusher,
        "_unregister_call_observer",
        lambda o: (_ for _ in ()).throw(RuntimeError("gone")),
    )
    usage_flusher._mark_registered_for_test()
    usage_flusher._set_heartbeat_for_test()
    usage_flusher.unregister()
    assert usage_flusher._is_registered() is False


def test_shutdown_waits_for_in_flight_heartbeat_flush(monkeypatch) -> None:
    """shutdown() must not return while a heartbeat ``write_rows`` is still running.

    BackgroundHeartbeat.stop() joins for a bounded timeout. If that join
    returns while ``_drain`` still holds the batch it popped, the lifecycle
    caller would otherwise close Postgres under the in-flight write.
    """
    started = threading.Event()
    release = threading.Event()
    written: list = []

    def slow_write(rows):
        started.set()
        assert release.wait(timeout=5)
        written.append(list(rows))
        return len(rows)

    monkeypatch.setattr(usage_flusher, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(usage_flusher.usage_store, "write_rows", slow_write)
    monkeypatch.setattr(usage_flusher, "_register_call_observer", lambda o: None)
    monkeypatch.setattr(usage_flusher, "_unregister_call_observer", lambda o: None)
    monkeypatch.setattr(usage_flusher, "_flush_interval_s", lambda: 0.1)

    usage_flusher.register_usage_flusher()
    assert usage_flusher._heartbeat is not None
    usage_flusher._heartbeat._join_timeout = 0.15
    usage_flusher._usage_observer(_Rec())
    assert started.wait(timeout=2), "heartbeat should pick up the buffered row"

    finished = threading.Event()

    def _shutdown() -> None:
        usage_flusher.shutdown()
        finished.set()

    t = threading.Thread(target=_shutdown, name="usage-shutdown")
    try:
        t.start()
        # stop() times out at 0.15s; without waiting on the in-flight write,
        # shutdown would return immediately after that. 0.5s is enough for
        # the join timeout to elapse but not for a hung write.
        assert not finished.wait(timeout=0.5), "shutdown returned while write_rows was blocked"
        assert t.is_alive()
        release.set()
        t.join(timeout=2)
        assert not t.is_alive()
        assert finished.is_set()
        assert len(written) == 1
    finally:
        release.set()
        t.join(timeout=2)


def test_real_wrappers_register_and_unregister_with_llm_service() -> None:
    from llm_service import telemetry as tel

    sentinel = object()
    before = sentinel in tel._observers
    usage_flusher._register_call_observer(sentinel)
    assert sentinel in tel._observers
    assert before is False
    usage_flusher._unregister_call_observer(sentinel)
    assert sentinel not in tel._observers
