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


class _FakeExecClient:
    """A client whose ``execute_workflow`` records its call and resolves to a result."""

    def __init__(self, captured: dict, result: object) -> None:
        self._captured = captured
        self._result = result

    async def execute_workflow(self, workflow_run, *, args, id, task_queue):
        self._captured.update(workflow_run=workflow_run, args=args, id=id, task_queue=task_queue)
        return self._result


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


def test_start_workflow_sync_requires_non_empty_ids():
    """start_workflow_sync's docstring documents non-empty workflow_id/task_queue
    as preconditions, matching its execute/signal/cancel siblings — enforce them
    the same way. The asserts precede the client wait, so no worker is needed."""
    with pytest.raises(AssertionError):
        runner.start_workflow_sync(object(), workflow_id="", task_queue="q")
    with pytest.raises(AssertionError):
        runner.start_workflow_sync(object(), workflow_id="wid", task_queue="")


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


def test_execute_workflow_sync_returns_result(running_loop):
    """The execute-and-wait bridge starts the workflow and returns its result."""
    captured: dict = {}
    sentinel = {"job_id": "j1"}
    client_mod.set_temporal_client(_FakeExecClient(captured, sentinel))
    client_mod.set_temporal_loop(running_loop)

    out = runner.execute_workflow_sync(
        object(), "a1", {"k": "v"}, workflow_id="wid", task_queue="q"
    )

    assert out is sentinel
    assert captured["id"] == "wid"
    assert captured["task_queue"] == "q"
    assert captured["args"] == ["a1", {"k": "v"}]


def test_execute_workflow_sync_raises_when_no_worker():
    """With no connected client/loop the execute bridge surfaces a clear worker error."""
    prev_c, prev_l = client_mod.get_temporal_client(), client_mod.get_temporal_loop()
    client_mod.set_temporal_client(None)
    client_mod.set_temporal_loop(None)
    try:
        with pytest.raises(RuntimeError, match="worker"):
            runner.execute_workflow_sync(
                object(), workflow_id="wid", task_queue="q", client_ready_timeout_s=0.05
            )
    finally:
        client_mod.set_temporal_client(prev_c)
        client_mod.set_temporal_loop(prev_l)


def test_execute_requires_non_empty_ids():
    """execute_workflow_sync asserts non-empty workflow_id/task_queue like the
    signal/cancel bridges. The asserts precede the client wait, so no worker is needed."""
    with pytest.raises(AssertionError):
        runner.execute_workflow_sync(object(), workflow_id="", task_queue="q")
    with pytest.raises(AssertionError):
        runner.execute_workflow_sync(object(), workflow_id="wid", task_queue="")


def test_bridges_are_exported():
    import shared_temporal

    assert shared_temporal.signal_workflow_sync is runner.signal_workflow_sync
    assert shared_temporal.cancel_workflow_sync is runner.cancel_workflow_sync
    assert shared_temporal.execute_workflow_sync is runner.execute_workflow_sync
    assert shared_temporal.execute_workflow_async is runner.execute_workflow_async


# ---------------------------------------------------------------------------
# execute_workflow_async — non-blocking execute-and-wait for async callers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_workflow_async_returns_result(running_loop):
    """The async bridge schedules on the worker loop and awaits the result
    without blocking the caller's loop."""
    captured: dict = {}
    sentinel = {"handle": "warm"}
    client_mod.set_temporal_client(_FakeExecClient(captured, sentinel))
    client_mod.set_temporal_loop(running_loop)

    out = await runner.execute_workflow_async(object(), "a1", workflow_id="wid", task_queue="q")

    assert out is sentinel
    assert captured["id"] == "wid"
    assert captured["task_queue"] == "q"
    assert captured["args"] == ["a1"]


@pytest.mark.asyncio
async def test_execute_workflow_async_requires_non_empty_ids():
    with pytest.raises(AssertionError):
        await runner.execute_workflow_async(object(), workflow_id="", task_queue="q")
    with pytest.raises(AssertionError):
        await runner.execute_workflow_async(object(), workflow_id="wid", task_queue="")


@pytest.mark.asyncio
async def test_execute_workflow_async_raises_when_no_worker():
    prev_c, prev_l = client_mod.get_temporal_client(), client_mod.get_temporal_loop()
    client_mod.set_temporal_client(None)
    client_mod.set_temporal_loop(None)
    try:
        with pytest.raises(RuntimeError, match="worker"):
            await runner.execute_workflow_async(
                object(), workflow_id="wid", task_queue="q", client_ready_timeout_s=0.05
            )
    finally:
        client_mod.set_temporal_client(prev_c)
        client_mod.set_temporal_loop(prev_l)


class _FakeSlowClient:
    """A client whose ``execute_workflow`` never resolves within the test's timeout."""

    async def execute_workflow(self, workflow_run, *, args, id, task_queue):
        await asyncio.sleep(10)


@pytest.mark.asyncio
async def test_execute_workflow_async_times_out(running_loop):
    """``execute_timeout_s`` bounds the caller's wait: a workflow that outlives
    it raises TimeoutError instead of hanging the caller forever (the workflow
    itself keeps running server-side — Temporal is durable — this only stops
    the caller's own wait, per the docstring's 'best-effort: abandon the
    cross-loop future' comment)."""
    client_mod.set_temporal_client(_FakeSlowClient())
    client_mod.set_temporal_loop(running_loop)

    with pytest.raises(asyncio.TimeoutError):
        await runner.execute_workflow_async(
            object(), workflow_id="wid", task_queue="q", execute_timeout_s=0.1
        )
