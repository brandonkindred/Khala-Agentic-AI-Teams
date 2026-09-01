"""Unit tests for the batched SE trace flusher (``trace_flusher``).

The flusher moves ``se_agent_traces`` INSERTs off the LLM call path: the
observer builds a row tuple (pure Python, no I/O) and appends to a bounded
deque; a background heartbeat drains the deque via ``executemany``. These
tests pin the three properties from the refactor spec — buffer/overflow
semantics, the 16-column INSERT order, and zero DB I/O on enqueue.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from software_engineering_team.shared import trace_flusher, trace_store


class _Rec:
    """Minimal stand-in for ``llm_service.telemetry.LLMCallRecord``."""

    def __init__(self, **overrides: Any) -> None:
        self.timestamp = datetime.now(tz=timezone.utc).timestamp()
        self.team = "software_engineering"
        self.agent_key = "backend"
        self.job_id = "j9"
        self.task_id = "t1"
        self.phase = "execution"
        self.model = "deepseek-v4-pro:cloud"
        self.prompt_tokens = 1000
        self.completion_tokens = 500
        self.total_tokens = 1500
        self.cost_usd = 0.42
        self.latency_ms = 1200
        self.status = "success"
        self.outcome = "success"
        self.objective = "write code"
        self.request_id = "rid1"
        for k, v in overrides.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _reset_flusher(monkeypatch):
    """Start each test with an empty buffer, no registered observer/heartbeat,
    and the trace sink enabled (SE_TRACE_TO_POSTGRES=1) so the observer exercises
    the enqueue path by default; tests that want the disabled-sink path delenv it."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "1")
    trace_flusher._reset_for_test()
    yield
    trace_flusher._reset_for_test()


def test_observer_no_db_io_on_enqueue(monkeypatch) -> None:
    """The observer never touches Postgres — enqueuing is pure Python.

    Regression for the whole point of the refactor: every LLM call used to do
    a blocking single-row INSERT before returning. The observer must now build
    a row tuple and append to the deque with zero DB I/O.
    """
    # Any pg_cursor acquisition on the call path is a hard failure.
    monkeypatch.setattr(
        trace_store,
        "pg_cursor",
        lambda *a, **k: pytest.fail("observer must not open a DB cursor on enqueue"),
    )
    monkeypatch.setattr(
        trace_flusher.trace_store,
        "pg_cursor",
        lambda *a, **k: pytest.fail("observer must not open a DB cursor on enqueue"),
    )

    trace_flusher._trace_observer(_Rec())  # must not raise / touch the DB
    assert trace_flusher._buffer_size() == 1


def test_observer_ignores_non_se_and_missing_job() -> None:
    """Only SE-team records with a job_id are enqueued (mirrors the old observer)."""
    trace_flusher._trace_observer(_Rec(team="blogging"))
    trace_flusher._trace_observer(_Rec(job_id=""))
    assert trace_flusher._buffer_size() == 0

    trace_flusher._trace_observer(_Rec(team="software_engineering_team"))
    assert trace_flusher._buffer_size() == 1


def test_observer_skips_when_sink_disabled(monkeypatch) -> None:
    """When SE_TRACE_TO_POSTGRES is explicitly opted out, the observer skips the
    per-call _record_to_row + enqueue work — the drain would drop these rows
    anyway (write_rows re-checks the flag), and on a high-throughput job
    buffering them would fill the buffer and emit drop warnings for rows that
    are never persisted."""
    monkeypatch.setenv("SE_TRACE_TO_POSTGRES", "false")
    trace_flusher._trace_observer(_Rec())
    assert trace_flusher._buffer_size() == 0


def test_overflow_warning_throttled_to_once_per_burst(monkeypatch, caplog) -> None:
    """A sustained over-cap burst logs the overflow WARNING once, not on every
    over-cap call. The throttle flag is set on the first overflow and only
    resets once a non-overflow call drops the buffer below cap, so a DB outage
    or sustained burst cannot flood the log."""
    monkeypatch.setenv("SE_TRACE_BUFFER_MAX", "2")
    caplog.set_level("WARNING", logger="software_engineering_team.shared.trace_flusher")

    for i in range(5):
        trace_flusher._trace_observer(_Rec(task_id=f"t{i}"))

    warnings = [r for r in caplog.records if "dropping oldest" in r.message]
    assert len(warnings) == 1  # only the first over-cap call warns
    # Cap is 2: t3, t4 remain after the five-call burst.
    assert trace_flusher._buffer_size() == 2
    assert [r[4] for r in trace_flusher._snapshot_buffer()] == ["t3", "t4"]

    # The throttle stays armed while overflow continues: a further over-cap call
    # does not warn again (the flag only resets on a non-overflow call).
    caplog.clear()
    trace_flusher._trace_observer(_Rec(task_id="t9"))  # buffer at cap → still overflows
    warnings2 = [r for r in caplog.records if "dropping oldest" in r.message]
    assert len(warnings2) == 0


def test_buffer_overflow_drops_oldest_and_warns(monkeypatch, caplog) -> None:
    """A full buffer drops the oldest row (bounded memory) and logs a WARNING."""
    monkeypatch.setenv("SE_TRACE_BUFFER_MAX", "3")
    caplog.set_level("WARNING", logger="software_engineering_team.shared.trace_flusher")

    for i in range(5):
        trace_flusher._trace_observer(_Rec(task_id=f"t{i}"))

    # Cap is 3: the two oldest (t0, t1) were dropped; t2..t4 remain, in order.
    assert trace_flusher._buffer_size() == 3
    rows = trace_flusher._snapshot_buffer()
    # Column 4 (0-indexed) is task_id — see test_drain_column_order for the full map.
    assert [r[4] for r in rows] == ["t2", "t3", "t4"]
    assert any("dropping oldest" in r.message for r in caplog.records)


def test_drain_column_order_matches_insert(monkeypatch) -> None:
    """Drain writes rows in the exact 16-column INSERT order from trace_store.

    Pins column order + positional params so a future edit can't silently shift
    a field. The drained tuples must equal ``trace_store._record_to_row(rec)``.
    """
    captured: list[tuple] = []

    def fake_write_rows(rows):
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr(trace_store, "write_rows", fake_write_rows)
    # Drain must call trace_store.write_rows (not pg_cursor), so guard it too.
    monkeypatch.setattr(
        trace_flusher.trace_store,
        "pg_cursor",
        lambda *a, **k: pytest.fail("drain should route through write_rows"),
    )

    rec = _Rec(task_id="tX", job_id="jZ", model="m", cost_usd=1.5)
    expected = trace_store._record_to_row(rec)
    trace_flusher._trace_observer(rec)
    n = trace_flusher.drain()

    assert n == 1
    assert captured == [expected]
    # The buffer is empty after a successful drain.
    assert trace_flusher._buffer_size() == 0


def test_drain_preserves_order_and_batches(monkeypatch) -> None:
    """Drain flushes all buffered rows in insertion order in one executemany."""
    captured: list[tuple] = []
    monkeypatch.setattr(trace_store, "write_rows", lambda rows: captured.extend(rows) or len(rows))

    for i in range(4):
        trace_flusher._trace_observer(_Rec(task_id=f"t{i}"))
    n = trace_flusher.drain()

    assert n == 4
    assert [r[4] for r in captured] == ["t0", "t1", "t2", "t3"]


def test_drain_swallows_write_failure(monkeypatch, caplog) -> None:
    """A write_rows failure never raises into the caller — rows stay dropped."""
    caplog.set_level("DEBUG", logger="software_engineering_team.shared.trace_flusher")
    monkeypatch.setattr(
        trace_store, "write_rows", lambda rows: (_ for _ in ()).throw(RuntimeError("pg down"))
    )
    trace_flusher._trace_observer(_Rec())
    # Must not raise.
    n = trace_flusher.drain()
    assert n == 0
    # Failure was logged, not raised.
    assert any("failed to flush" in r.message for r in caplog.records)


def test_register_starts_heartbeat_and_registers_observer(monkeypatch) -> None:
    """register_trace_flusher wires the observer + background heartbeat (idempotent)."""
    registered: list = []
    monkeypatch.setattr(trace_flusher, "_register_call_observer", registered.append, raising=False)
    started: list = []

    real_start = trace_flusher.BackgroundHeartbeat.start

    def fake_start(self):
        started.append(self)
        return real_start(self)

    monkeypatch.setattr(trace_flusher.BackgroundHeartbeat, "start", fake_start)

    trace_flusher.register_trace_flusher()
    trace_flusher.register_trace_flusher()  # idempotent — second is a no-op

    assert registered == [trace_flusher._trace_observer]
    assert len(started) == 1
    assert trace_flusher._is_registered()

    # Clean up the started heartbeat so it doesn't outlive the test.
    trace_flusher._reset_for_test()


def test_unregister_stops_heartbeat_and_removes_observer(monkeypatch) -> None:
    """unregister stops the heartbeat and removes the observer from llm_service."""
    unregistered: list = []
    monkeypatch.setattr(
        trace_flusher, "_unregister_call_observer", unregistered.append, raising=False
    )
    # Pretend registration already happened so unregister has work to do.
    trace_flusher._mark_registered_for_test()
    trace_flusher._set_heartbeat_for_test()

    trace_flusher.unregister()

    assert unregistered == [trace_flusher._trace_observer]
    assert not trace_flusher._is_registered()


def test_shutdown_drains_then_stops(monkeypatch) -> None:
    """Lifecycle shutdown flushes remaining rows before unregistering."""
    drained = {"called": False}
    monkeypatch.setattr(trace_flusher, "drain", lambda: drained.__setitem__("called", True) or 0)
    monkeypatch.setattr(trace_flusher, "unregister", lambda: None)

    trace_flusher.shutdown()

    assert drained["called"] is True


def test_drain_empty_buffer_is_zero() -> None:
    """drain() on an empty buffer is a no-op returning 0 (no DB call)."""
    assert trace_flusher.drain() == 0


def test_unregister_when_not_registered_is_noop() -> None:
    """unregister() on a fresh (unregistered) state is a no-op, not an error."""
    # Fresh reset state — _registered is False.
    trace_flusher.unregister()  # must not raise
    assert trace_flusher._is_registered() is False


def test_register_failure_does_not_register(monkeypatch, caplog) -> None:
    """If observer registration raises, we warn and stay unregistered (no heartbeat)."""
    caplog.set_level("WARNING", logger="software_engineering_team.shared.trace_flusher")

    def boom(observer):
        raise RuntimeError("llm_service unavailable")

    monkeypatch.setattr(trace_flusher, "_register_call_observer", boom, raising=False)

    trace_flusher.register_trace_flusher()

    assert trace_flusher._is_registered() is False
    assert trace_flusher._heartbeat is None
    assert any("could not register" in r.message for r in caplog.records)


def test_unregister_failure_is_swallowed(monkeypatch) -> None:
    """A failure removing the observer is logged, not raised."""
    monkeypatch.setattr(
        trace_flusher,
        "_unregister_call_observer",
        lambda o: (_ for _ in ()).throw(RuntimeError),
        raising=False,
    )
    trace_flusher._mark_registered_for_test()
    trace_flusher._set_heartbeat_for_test()

    trace_flusher.unregister()  # must not raise
    assert trace_flusher._is_registered() is False

    trace_flusher._reset_for_test()


def test_real_wrappers_register_and_unregister_with_llm_service() -> None:
    """The thin wrappers delegate to llm_service's real observer registry."""
    from llm_service import telemetry as tel

    sentinel = object()
    before = sentinel in tel._observers

    trace_flusher._register_call_observer(sentinel)
    assert sentinel in tel._observers
    assert before is False

    trace_flusher._unregister_call_observer(sentinel)
    assert sentinel not in tel._observers
