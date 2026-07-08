"""Tests for the sync signal/cancel bridges added to shared_temporal.runner.

``signal_workflow_sync`` / ``cancel_workflow_sync`` resolve the workflow handle from
the shared client and schedule the async call on the worker loop via
``run_coroutine_threadsafe`` — the same shape as ``start_workflow_sync``. These tests
run a real event loop in a background thread (mimicking the worker) with a fake client,
so no Temporal server is needed.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import shared_temporal.client as client_mod
import shared_temporal.runner as runner


class _FakeHandle:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    async def signal(self, name, *args):
        self._captured["signal"] = (name, args)

    async def cancel(self):
        self._captured["cancel"] = True


class _FakeClient:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def get_workflow_handle(self, workflow_id):
        self._captured["workflow_id"] = workflow_id
        return _FakeHandle(self._captured)


@pytest.fixture
def running_loop():
    """A real event loop running in a background thread + restored globals."""
    prev_c, prev_l = client_mod.get_temporal_client(), client_mod.get_temporal_loop()
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(timeout=5)
        loop.close()
        client_mod.set_temporal_client(prev_c)
        client_mod.set_temporal_loop(prev_l)


def test_signal_workflow_sync_delivers_signal(running_loop):
    captured: dict = {}
    client_mod.set_temporal_client(_FakeClient(captured))
    client_mod.set_temporal_loop(running_loop)

    runner.signal_workflow_sync("wf-1", "submit_input", "the answer")

    assert captured["workflow_id"] == "wf-1"
    assert captured["signal"] == ("submit_input", ("the answer",))


def test_cancel_workflow_sync_requests_cancel(running_loop):
    captured: dict = {}
    client_mod.set_temporal_client(_FakeClient(captured))
    client_mod.set_temporal_loop(running_loop)

    runner.cancel_workflow_sync("wf-2")

    assert captured["workflow_id"] == "wf-2"
    assert captured["cancel"] is True


def test_signal_requires_non_empty_ids(running_loop):
    client_mod.set_temporal_client(_FakeClient({}))
    client_mod.set_temporal_loop(running_loop)
    with pytest.raises(AssertionError):
        runner.signal_workflow_sync("", "submit_input")
    with pytest.raises(AssertionError):
        runner.signal_workflow_sync("wf", "")


def test_signal_raises_when_no_worker():
    """With no connected client/loop the bridge surfaces a clear worker error."""
    prev_c, prev_l = client_mod.get_temporal_client(), client_mod.get_temporal_loop()
    client_mod.set_temporal_client(None)
    client_mod.set_temporal_loop(None)
    try:
        with pytest.raises(RuntimeError, match="worker"):
            runner.signal_workflow_sync("wf", "submit_input", client_ready_timeout_s=0.05)
        with pytest.raises(RuntimeError, match="worker"):
            runner.cancel_workflow_sync("wf", client_ready_timeout_s=0.05)
    finally:
        client_mod.set_temporal_client(prev_c)
        client_mod.set_temporal_loop(prev_l)


def test_bridges_are_exported():
    import shared_temporal

    assert shared_temporal.signal_workflow_sync is runner.signal_workflow_sync
    assert shared_temporal.cancel_workflow_sync is runner.cancel_workflow_sync
