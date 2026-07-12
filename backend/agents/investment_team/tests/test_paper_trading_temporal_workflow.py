"""Unit tests for ``PaperTradingWorkflow`` — the durable paper-trading driver.

Driven with ``asyncio.run`` and patched ``temporalio.workflow`` primitives
(``start_activity`` / ``wait_condition`` / ``execute_activity``) — no live
server. Covers the happy path, the ``stop`` signal → cancel → compensation
(persist terminal state for a cancel-before-start) → ``stopped`` result, and the
``status`` query.
"""

from __future__ import annotations

import asyncio

import pytest


class _Handle:
    """Fake started-activity handle (an asyncio.Task stand-in)."""

    def __init__(
        self, *, done: bool, result: dict | None = None, raise_on_await: bool = False
    ) -> None:
        self._done = done
        self._result = result
        self._raise = raise_on_await
        self.cancelled = False

    def done(self) -> bool:
        return self._done

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self):
        async def _c() -> dict:
            if self._raise:
                raise asyncio.CancelledError()
            return self._result

        return _c().__await__()


def _patch_wait_condition(monkeypatch) -> None:
    async def _fake_wait(predicate, *a, **k) -> None:
        for _ in range(1000):
            if predicate():
                return
            await asyncio.sleep(0)

    monkeypatch.setattr("temporalio.workflow.wait_condition", _fake_wait)


def test_workflow_returns_activity_result(monkeypatch) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    handle = _Handle(done=True, result={"session_id": "pt-1", "status": "completed"})
    monkeypatch.setattr(tl_workflow, "start_activity", lambda fn, *, args, **kw: handle)
    _patch_wait_condition(monkeypatch)

    wf = PaperTradingWorkflow()
    result = asyncio.run(wf.run({"session_id": "pt-1", "max_hours": 1.0}))

    assert result == {"session_id": "pt-1", "status": "completed"}
    assert wf.status() == "completed"
    assert handle.cancelled is False


def test_workflow_defaults_max_hours_when_absent(monkeypatch) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    captured = {}

    def _start(fn, *, args, **kw):
        captured.update(kw)
        return _Handle(done=True, result={"session_id": "pt-1", "status": "completed"})

    monkeypatch.setattr(tl_workflow, "start_activity", _start)
    _patch_wait_condition(monkeypatch)

    asyncio.run(PaperTradingWorkflow().run({"session_id": "pt-1"}))
    assert captured["start_to_close_timeout"].total_seconds() > 72 * 3600


def test_stop_signal_cancels_and_persists_terminal_state(monkeypatch) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    handle = _Handle(done=False, raise_on_await=True)
    monkeypatch.setattr(tl_workflow, "start_activity", lambda fn, *, args, **kw: handle)
    _patch_wait_condition(monkeypatch)

    compensation = []

    async def _fake_exec(fn, *, args, **kw):
        compensation.append((fn.__name__, args))
        return {"session_id": "pt-1", "status": "failed"}

    monkeypatch.setattr(tl_workflow, "execute_activity", _fake_exec)

    wf = PaperTradingWorkflow()
    # Signal already delivered (the handler only flips the flag): the run loop's
    # wait_condition observes it and takes the cancel + compensation branch. This
    # is the cancel-before-start window the compensation activity exists for.
    wf.stop()

    result = asyncio.run(wf.run({"session_id": "pt-1"}))

    assert result == {"session_id": "pt-1", "status": "stopped"}
    assert wf.status() == "stopped"
    assert handle.cancelled is True
    # The compensation activity ran so a cancel-before-start still gets a
    # terminal session state persisted.
    assert compensation == [("mark_paper_trading_stopped_activity", ["pt-1"])]


def test_stop_before_start_is_safe() -> None:
    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    wf = PaperTradingWorkflow()
    wf.stop()  # only sets the flag; no handle yet — must not raise
    assert wf._stop_requested is True
    assert wf.status() == "running"


@pytest.mark.parametrize("missing_status", ["completed", None])
def test_status_query_defaults_to_completed(monkeypatch, missing_status) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    payload_result = {"session_id": "pt-1"}
    if missing_status is not None:
        payload_result["status"] = missing_status
    monkeypatch.setattr(
        tl_workflow,
        "start_activity",
        lambda fn, *, args, **kw: _Handle(done=True, result=payload_result),
    )
    _patch_wait_condition(monkeypatch)

    wf = PaperTradingWorkflow()
    asyncio.run(wf.run({"session_id": "pt-1"}))
    assert wf.status() == (missing_status or "completed")
