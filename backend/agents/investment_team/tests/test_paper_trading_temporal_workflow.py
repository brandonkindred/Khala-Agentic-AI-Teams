"""Unit tests for ``PaperTradingWorkflow`` — the durable paper-trading driver.

Driven with ``asyncio.run`` and a patched ``temporalio.workflow.start_activity``
(no live server), matching the strategy-lab workflow test style. Covers the
happy path, the ``stop`` signal → activity cancellation → ``stopped`` result, and
the ``status`` query.
"""

from __future__ import annotations

import asyncio

import pytest


class _CompletedHandle:
    """A started-activity handle that resolves to a fixed result."""

    def __init__(self, result: dict) -> None:
        self._result = result
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self):
        async def _c() -> dict:
            return self._result

        return _c().__await__()


class _CancellableHandle:
    """A handle that blocks until cancelled, then raises ``CancelledError``."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self):
        async def _c() -> dict:
            while not self.cancelled:
                await asyncio.sleep(0)
            raise asyncio.CancelledError()

        return _c().__await__()


def test_workflow_returns_activity_result(monkeypatch) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    handle = _CompletedHandle({"session_id": "pt-1", "status": "completed"})
    monkeypatch.setattr(tl_workflow, "start_activity", lambda fn, *, args, **kw: handle)

    wf = PaperTradingWorkflow()
    result = asyncio.run(wf.run({"session_id": "pt-1", "max_hours": 1.0}))

    assert result == {"session_id": "pt-1", "status": "completed"}
    assert wf.status() == "completed"


def test_workflow_defaults_max_hours_when_absent(monkeypatch) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    captured = {}

    def _start(fn, *, args, **kw):
        captured.update(kw)
        return _CompletedHandle({"session_id": "pt-1", "status": "completed"})

    monkeypatch.setattr(tl_workflow, "start_activity", _start)

    asyncio.run(PaperTradingWorkflow().run({"session_id": "pt-1"}))
    # A wide ceiling derived from the default 72h guard + buffer.
    assert captured["start_to_close_timeout"].total_seconds() > 72 * 3600


def test_stop_signal_cancels_activity(monkeypatch) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    handle = _CancellableHandle()
    monkeypatch.setattr(tl_workflow, "start_activity", lambda fn, *, args, **kw: handle)

    wf = PaperTradingWorkflow()

    async def _drive() -> dict:
        task = asyncio.ensure_future(wf.run({"session_id": "pt-1"}))
        await asyncio.sleep(0.01)  # let run() start + await the activity
        wf.stop()  # cancels the handle
        return await task

    result = asyncio.run(_drive())

    assert handle.cancelled is True
    assert result == {"session_id": "pt-1", "status": "stopped"}
    assert wf.status() == "stopped"


def test_stop_before_start_is_safe() -> None:
    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    wf = PaperTradingWorkflow()
    # No activity handle yet — must not raise.
    wf.stop()
    assert wf.status() == "running"


@pytest.mark.parametrize("missing_status", ["completed", None])
def test_status_query_defaults_to_completed(monkeypatch, missing_status) -> None:
    from temporalio import workflow as tl_workflow

    from investment_team.temporal.paper_trading import PaperTradingWorkflow

    payload_result = {"session_id": "pt-1"}
    if missing_status is not None:
        payload_result["status"] = missing_status
    monkeypatch.setattr(
        tl_workflow, "start_activity", lambda fn, *, args, **kw: _CompletedHandle(payload_result)
    )

    wf = PaperTradingWorkflow()
    asyncio.run(wf.run({"session_id": "pt-1"}))
    assert wf.status() == (missing_status or "completed")
