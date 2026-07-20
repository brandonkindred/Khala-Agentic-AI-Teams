"""Regression tests: a dead worker must not leave a usable-looking client/loop.

When a team's Temporal worker thread connects (populating the shared
``_client``/``_loop`` slots) and then exits — ``worker.run()`` raises, the
Temporal server drops the connection, the thread is cancelled — its event loop
is closed. The bug these tests pin down: the shared globals were left pointing at
that closed loop, and ``_await_client`` returned it unchecked, so
``start_workflow_sync`` submitted a coroutine to a closed loop and raised the
cryptic ``RuntimeError: Event loop is closed`` (plus a "coroutine was never
awaited" warning) instead of the intended clean "worker not running" failure.
"""

from __future__ import annotations

import asyncio

import pytest

import shared_temporal.client as client_mod
import shared_temporal.runner as runner
import shared_temporal.worker as worker_mod


@pytest.fixture(autouse=True)
def _reset_temporal_globals():
    """Isolate the module-level client/loop slots per test."""
    prev_c, prev_l = client_mod.get_temporal_client(), client_mod.get_temporal_loop()
    client_mod.set_temporal_client(None)
    client_mod.set_temporal_loop(None)
    yield
    client_mod.set_temporal_client(prev_c)
    client_mod.set_temporal_loop(prev_l)


def _make_closed_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()
    loop.close()
    return loop


def test_await_client_rejects_closed_loop():
    """A stale, closed loop must count as 'not ready', not be handed back."""
    client_mod.set_temporal_client(object())
    client_mod.set_temporal_loop(_make_closed_loop())

    with pytest.raises(RuntimeError, match="worker"):
        runner._await_client(timeout_s=0.05)


def test_start_workflow_sync_raises_clear_error_on_closed_loop():
    """The full sync bridge surfaces a clear error (not 'Event loop is closed')
    and never fabricates an un-awaited coroutine when the loop is dead."""

    class _FakeClient:
        def start_workflow(self, *_a, **_k):  # pragma: no cover - must not be reached
            raise AssertionError("start_workflow must not run against a closed loop")

    client_mod.set_temporal_client(_FakeClient())
    client_mod.set_temporal_loop(_make_closed_loop())

    with pytest.raises(RuntimeError, match="worker"):
        runner.start_workflow_sync(
            object(),
            workflow_id="wf-1",
            task_queue="q",
            client_ready_timeout_s=0.05,
        )


def test_worker_thread_clears_globals_on_exit(monkeypatch):
    """When a connected worker thread dies, it must clear the shared slots so a
    later dispatch waits/fails clearly instead of using the dead loop."""
    monkeypatch.setattr(worker_mod, "is_temporal_enabled", lambda: True)

    captured: dict = {}

    async def _fake_run(team, queue, workflows, activities, mca):
        # Mimic a worker that connected (claimed the shared slots) then died.
        loop = asyncio.get_running_loop()
        client_mod.set_temporal_client(object())
        client_mod.set_temporal_loop(loop)
        captured["loop"] = loop
        raise RuntimeError("worker died right after connecting")

    monkeypatch.setattr(worker_mod, "_run_worker_async", _fake_run)
    worker_mod._worker_threads.pop("dead-team", None)

    assert worker_mod.start_team_worker("dead-team", [], []) is True
    worker_mod._worker_threads["dead-team"].join(timeout=5)

    assert captured["loop"].is_closed()
    assert client_mod.get_temporal_client() is None
    assert client_mod.get_temporal_loop() is None
