"""Tests for the sync signal/cancel bridges added to shared.temporal.runner.

``signal_workflow_sync`` / ``cancel_workflow_sync`` resolve the workflow handle from
the shared client and schedule the async call on the worker loop via
``run_coroutine_threadsafe`` — the same shape as ``start_workflow_sync``. These tests
run a real event loop in a background thread (mimicking the worker) with a fake client,
so no Temporal server is needed.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

import shared.temporal.client as client_mod
import shared.temporal.runner as runner


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


class _FakeTerminateHandle:
    """A handle whose ``terminate``/``result`` outcomes are configurable per test."""

    def __init__(self, captured: dict, *, terminate_exc=None, result_exc=None) -> None:
        self._captured = captured
        self._terminate_exc = terminate_exc
        self._result_exc = result_exc

    async def terminate(self, *, reason=None):
        self._captured["terminate_reason"] = reason
        if self._terminate_exc is not None:
            raise self._terminate_exc

    async def result(self):
        self._captured["result_called"] = True
        if self._result_exc is not None:
            raise self._result_exc


class _FakeTerminateClient:
    def __init__(self, captured: dict, handle: "_FakeTerminateHandle") -> None:
        self._captured = captured
        self._handle = handle

    def get_workflow_handle(self, workflow_id):
        self._captured["workflow_id"] = workflow_id
        return self._handle


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


# ---------------------------------------------------------------------------
# terminate_and_await_workflow_sync
# ---------------------------------------------------------------------------


def test_terminate_and_await_workflow_sync_confirms_via_failure_error(running_loop):
    """The happy path: terminate() succeeds and result() raises
    WorkflowFailureError (the expected shape of a terminated workflow's
    outcome) — confirming the execution is closed."""
    from temporalio.client import WorkflowFailureError

    captured: dict = {}
    handle = _FakeTerminateHandle(captured, result_exc=WorkflowFailureError(cause=RuntimeError("x")))
    client_mod.set_temporal_client(_FakeTerminateClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    runner.terminate_and_await_workflow_sync("wf-3", reason="restart requested")

    assert captured["workflow_id"] == "wf-3"
    assert captured["terminate_reason"] == "restart requested"
    assert captured["result_called"] is True


def test_terminate_and_await_workflow_sync_noop_when_terminate_not_found(running_loop):
    """No workflow exists under this id (already closed/GC'd) — terminate()
    itself 404s, which must be treated as already-terminal, not an error, and
    result() must never be called (nothing to wait on)."""
    from temporalio.service import RPCError, RPCStatusCode

    captured: dict = {}
    handle = _FakeTerminateHandle(captured, terminate_exc=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    client_mod.set_temporal_client(_FakeTerminateClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    runner.terminate_and_await_workflow_sync("wf-4")

    assert "result_called" not in captured


def test_terminate_and_await_workflow_sync_noop_when_result_not_found(running_loop):
    """The workflow closes/gets GC'd between terminate() accepting and our
    result() call landing — also treated as confirmed-terminal."""
    from temporalio.service import RPCError, RPCStatusCode

    captured: dict = {}
    handle = _FakeTerminateHandle(captured, result_exc=RPCError("not found", RPCStatusCode.NOT_FOUND, b""))
    client_mod.set_temporal_client(_FakeTerminateClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    runner.terminate_and_await_workflow_sync("wf-5")

    assert captured["result_called"] is True


def test_terminate_and_await_workflow_sync_propagates_other_terminate_errors(running_loop):
    from temporalio.service import RPCError, RPCStatusCode

    captured: dict = {}
    handle = _FakeTerminateHandle(captured, terminate_exc=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
    client_mod.set_temporal_client(_FakeTerminateClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    with pytest.raises(RPCError):
        runner.terminate_and_await_workflow_sync("wf-6")


def test_terminate_and_await_workflow_sync_propagates_other_result_errors(running_loop):
    from temporalio.service import RPCError, RPCStatusCode

    captured: dict = {}
    handle = _FakeTerminateHandle(captured, result_exc=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b""))
    client_mod.set_temporal_client(_FakeTerminateClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    with pytest.raises(RPCError):
        runner.terminate_and_await_workflow_sync("wf-7")


def test_terminate_and_await_workflow_sync_requires_non_empty_id(running_loop):
    client_mod.set_temporal_client(_FakeClient({}))
    client_mod.set_temporal_loop(running_loop)
    with pytest.raises(AssertionError):
        runner.terminate_and_await_workflow_sync("")


def test_terminate_and_await_workflow_sync_raises_when_no_worker():
    prev_c, prev_l = client_mod.get_temporal_client(), client_mod.get_temporal_loop()
    client_mod.set_temporal_client(None)
    client_mod.set_temporal_loop(None)
    try:
        with pytest.raises(RuntimeError, match="worker"):
            runner.terminate_and_await_workflow_sync("wf", client_ready_timeout_s=0.05)
    finally:
        client_mod.set_temporal_client(prev_c)
        client_mod.set_temporal_loop(prev_l)


def test_terminate_and_await_workflow_sync_times_out(running_loop):
    """A result() that never resolves within timeout_s must raise TimeoutError
    (the terminate request was still sent — the caller decides how to handle
    'not yet confirmed done'), not hang the caller forever."""

    class _SlowHandle(_FakeTerminateHandle):
        async def result(self):
            self._captured["result_called"] = True
            await asyncio.sleep(10)

    captured: dict = {}
    handle = _SlowHandle(captured)
    client_mod.set_temporal_client(_FakeTerminateClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    with pytest.raises(TimeoutError):
        runner.terminate_and_await_workflow_sync("wf-8", timeout_s=0.1)


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


# ---------------------------------------------------------------------------
# execute_workflow_sync — reattach_on_timeout
# ---------------------------------------------------------------------------


class _FakeSlowExecClient:
    """A client whose ``execute_workflow`` never resolves within the test's timeout."""

    def __init__(self, captured: "dict | None" = None) -> None:
        self._captured = captured

    async def execute_workflow(self, workflow_run, *, args, id, task_queue):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            if self._captured is not None:
                self._captured["execute_workflow_cancelled"] = True
            raise


class _FakeReattachHandle:
    """A workflow handle whose ``result()`` takes real wall-clock time to
    resolve, simulating a workflow that outlives several client-side wait
    windows before reaching its own terminal state. Tracks how many times
    ``result()`` itself is invoked (as opposed to how many windows the
    caller polls through) — the reattach loop must schedule this coroutine
    ONCE for the whole wait and re-poll that same future across windows,
    never abandon a still-running one and schedule a fresh one per window."""

    def __init__(self, captured: dict, *, delay_s: float, result: object) -> None:
        self._captured = captured
        self._delay_s = delay_s
        self._result = result

    async def result(self):
        self._captured["handle_result_calls"] = self._captured.get("handle_result_calls", 0) + 1
        await asyncio.sleep(self._delay_s)
        return self._result


class _FakeReattachClient(_FakeSlowExecClient):
    """``execute_workflow`` always hangs (as if the client-side wait always
    expires); ``get_workflow_handle`` returns the same still-running handle each
    time, matching Temporal's real semantics — reattaching never starts a new
    workflow."""

    def __init__(self, captured: dict, handle: "_FakeReattachHandle") -> None:
        self._captured = captured
        self._handle = handle

    def get_workflow_handle(self, workflow_id):
        self._captured["reattach_workflow_id"] = workflow_id
        self._captured["get_workflow_handle_calls"] = self._captured.get("get_workflow_handle_calls", 0) + 1
        return self._handle


def test_execute_workflow_sync_without_reattach_raises_timeout(running_loop):
    """Default behavior (reattach_on_timeout=False, unchanged) still raises on a
    client-side timeout rather than waiting indefinitely."""
    client_mod.set_temporal_client(_FakeSlowExecClient())
    client_mod.set_temporal_loop(running_loop)

    with pytest.raises(TimeoutError):
        runner.execute_workflow_sync(
            object(), workflow_id="wid", task_queue="q", execute_timeout_s=0.05
        )


def test_execute_workflow_sync_reattach_returns_result_after_timeout(running_loop):
    """reattach_on_timeout=True: a client-side timeout reattaches to the same
    workflow id (via get_workflow_handle, never starting a new run) and returns
    its eventual result instead of raising."""
    captured: dict = {}
    sentinel = {"ok": True}
    handle = _FakeReattachHandle(captured, delay_s=0.02, result=sentinel)
    client_mod.set_temporal_client(_FakeReattachClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    out = runner.execute_workflow_sync(
        object(),
        workflow_id="wid",
        task_queue="q",
        execute_timeout_s=0.01,
        reattach_on_timeout=True,
    )

    assert out is sentinel
    assert captured["reattach_workflow_id"] == "wid"


def test_execute_workflow_sync_reattach_cancels_initial_waiter(running_loop):
    """P1 regression: reattaching must cancel the abandoned initial
    `execute_workflow` waiter, not leave it running alongside the new
    reattach waiter — otherwise two coroutines both long-poll Temporal for
    the same workflow for its remaining lifetime, leaking a connection and
    degrading the shared worker event loop."""
    captured: dict = {}
    sentinel = {"ok": True}
    handle = _FakeReattachHandle(captured, delay_s=0.02, result=sentinel)
    client_mod.set_temporal_client(_FakeReattachClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    out = runner.execute_workflow_sync(
        object(),
        workflow_id="wid",
        task_queue="q",
        execute_timeout_s=0.01,
        reattach_on_timeout=True,
    )

    assert out is sentinel
    # Give the cancelled coroutine's CancelledError handler a moment to run on
    # the worker loop before asserting on it from this thread. A bounded poll
    # (rather than one fixed sleep) avoids flaking under CI load while still
    # failing promptly if the flag is never set.
    deadline = time.monotonic() + 2.0
    while captured.get("execute_workflow_cancelled") is not True and time.monotonic() < deadline:
        time.sleep(0.01)
    assert captured.get("execute_workflow_cancelled") is True


def test_execute_workflow_sync_reattach_polls_multiple_windows(running_loop):
    """A workflow that outlives several reattach windows is still waited on —
    reattach_on_timeout never gives up early — and does so by re-polling ONE
    scheduled ``handle.result()`` coroutine across every window, not by
    abandoning a still-running waiter and scheduling a fresh one each time
    (which would leak concurrent waiters for a long-running workflow).

    ``execute_workflow_sync`` is called on a background thread with a bounded
    ``join`` rather than directly on the test's own thread: a regression that
    made reattach give up too early would still pass eventually if it instead
    hung forever (e.g. re-polling the same already-cancelled waiter), and a
    bare hanging call here would only fail at CI's own global timeout with no
    clue which test caused it. The bound below fails fast with a clear
    message instead.
    """
    captured: dict = {}
    sentinel = {"ok": True}
    # Outlives several short client-side polling windows before resolving.
    handle = _FakeReattachHandle(captured, delay_s=0.1, result=sentinel)
    client_mod.set_temporal_client(_FakeReattachClient(captured, handle))
    client_mod.set_temporal_loop(running_loop)

    result_box: dict = {}

    def _call() -> None:
        try:
            result_box["out"] = runner.execute_workflow_sync(
                object(),
                workflow_id="wid",
                task_queue="q",
                execute_timeout_s=0.02,
                reattach_on_timeout=True,
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced via result_box, not lost silently
            result_box["exc"] = exc

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(timeout=5.0)
    assert not worker.is_alive(), (
        "execute_workflow_sync(reattach_on_timeout=True) did not return within the 5s bound -- "
        "reattach appears to have hung instead of giving up early or completing"
    )
    if "exc" in result_box:
        raise result_box["exc"]
    out = result_box["out"]

    assert out is sentinel
    # Exactly one handle lookup and one result() coroutine for the whole
    # wait, no matter how many client-side polling windows it spanned.
    assert captured["get_workflow_handle_calls"] == 1
    assert captured["handle_result_calls"] == 1


def test_bridges_are_exported():
    import shared.temporal

    assert shared.temporal.signal_workflow_sync is runner.signal_workflow_sync
    assert shared.temporal.cancel_workflow_sync is runner.cancel_workflow_sync
    assert shared.temporal.execute_workflow_sync is runner.execute_workflow_sync
    assert shared.temporal.execute_workflow_async is runner.execute_workflow_async
    assert shared.temporal.terminate_and_await_workflow_sync is runner.terminate_and_await_workflow_sync


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


@pytest.mark.asyncio
async def test_execute_workflow_async_cancelled_abandons_cross_loop_future(running_loop):
    """Cancelling the caller's own task while ``execute_workflow_async`` is awaiting
    the workflow result must re-raise ``CancelledError`` (not swallow it or raise
    something else) and must not itself blow up calling ``fut.cancel()`` on the
    still-running cross-loop future — the 'best-effort: abandon' path the
    docstring describes, exercised here via task cancellation rather than a timeout."""
    client_mod.set_temporal_client(_FakeSlowClient())
    client_mod.set_temporal_loop(running_loop)

    task = asyncio.ensure_future(
        runner.execute_workflow_async(object(), workflow_id="wid", task_queue="q")
    )
    await asyncio.sleep(0.05)  # let the coroutine reach its `await asyncio.wait_for(...)`
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_execute_workflow_async_uses_dedicated_client_wait_executor(running_loop, monkeypatch):
    """Regression: the client-ready poll previously ran via ``asyncio.to_thread``,
    which always uses the running loop's *default* executor — a burst of concurrent
    cold-start callers could exhaust threads shared with unrelated ``to_thread``
    work elsewhere in the process. It must instead run on shared.temporal's own
    dedicated pool (``runner._get_client_wait_executor``), identifiable by its
    ``temporal-client-wait`` thread name prefix."""
    import threading

    captured: dict = {}
    sentinel = {"ok": True}
    orig_get_client = runner.get_temporal_client

    def _recording_get_client():
        captured["thread_name"] = threading.current_thread().name
        return orig_get_client()

    # Patch the name bound inside `runner` (not `client_mod`) — `_await_client`
    # calls the function via its own `from shared.temporal.client import
    # get_temporal_client` binding, so patching the origin module's attribute
    # would not intercept calls made through runner's already-bound name.
    monkeypatch.setattr(runner, "get_temporal_client", _recording_get_client)
    client_mod.set_temporal_client(_FakeExecClient({}, sentinel))
    client_mod.set_temporal_loop(running_loop)

    out = await runner.execute_workflow_async(object(), workflow_id="wid", task_queue="q")

    assert out is sentinel
    assert captured["thread_name"].startswith("temporal-client-wait")
