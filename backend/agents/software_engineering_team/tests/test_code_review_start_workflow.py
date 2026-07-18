"""Tests for execute_code_review_workflow_sync — the synchronous execute-and-wait
bridge ``CodeReviewAgent._run_via_temporal`` uses to run ``CodeReviewWorkflow``.

Unlike the rest of this team's Temporal dispatch (which delegates to
``shared_temporal.runner.start_workflow_sync``), the code review agent hand-rolls
its own execute-and-wait bridge (``_await_client`` + ``run_coroutine_threadsafe``)
because ``CodeReviewAgent.run`` must return synchronously. These tests are modeled
on ``shared_temporal/tests/test_workflow_bridges.py``'s ``running_loop`` fixture: a
real event loop runs in a background thread (mimicking the worker) with a fake
client, so no live Temporal server is needed.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta

import pytest
from code_review_agent.temporal import config as cfg
from code_review_agent.temporal.start_workflow import execute_code_review_workflow_sync

import shared_temporal.client as client_mod


class _FakeExecClient:
    """A client whose ``execute_workflow`` records its call and resolves to a result."""

    def __init__(self, captured: dict, result: object) -> None:
        self._captured = captured
        self._result = result

    async def execute_workflow(
        self, workflow_run, review_input, *, id, task_queue, execution_timeout=None
    ):
        self._captured.update(
            workflow_run=workflow_run,
            review_input=review_input,
            id=id,
            task_queue=task_queue,
            execution_timeout=execution_timeout,
        )
        return self._result


class _FakeSlowClient:
    """A client whose ``execute_workflow`` never resolves within the test's timeout."""

    async def execute_workflow(
        self, workflow_run, review_input, *, id, task_queue, execution_timeout=None
    ):
        await asyncio.sleep(10)


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


def _review_input() -> dict:
    return {"code": "def foo(): return 1", "task_description": "t", "language": "python"}


def test_execute_code_review_workflow_sync_returns_result(running_loop) -> None:
    captured: dict = {}
    sentinel = {"approved": True, "issues": [], "summary": "ok", "spec_compliance_notes": ""}
    client_mod.set_temporal_client(_FakeExecClient(captured, sentinel))
    client_mod.set_temporal_loop(running_loop)

    out = execute_code_review_workflow_sync(_review_input(), workflow_id="wid-1")

    assert out is sentinel
    assert captured["id"] == "wid-1"
    assert captured["task_queue"] == cfg.TASK_QUEUE
    assert captured["review_input"] == _review_input()


def test_execute_code_review_workflow_sync_passes_derived_execution_timeout(
    running_loop,
) -> None:
    captured: dict = {}
    client_mod.set_temporal_client(_FakeExecClient(captured, {}))
    client_mod.set_temporal_loop(running_loop)

    execute_code_review_workflow_sync(_review_input(), workflow_id="wid-2", execute_timeout_s=3600)

    # Strictly below the client-side ceiling it was derived from, so the server
    # always reclaims an abandoned execution's worker slot before, not after,
    # this call's own wait gives up (see config.resolve_execution_timeout_s).
    assert captured["execution_timeout"] == timedelta(seconds=cfg.resolve_execution_timeout_s(3600))
    assert captured["execution_timeout"] < timedelta(seconds=3600)


def test_execute_code_review_workflow_sync_times_out(running_loop) -> None:
    client_mod.set_temporal_client(_FakeSlowClient())
    client_mod.set_temporal_loop(running_loop)

    with pytest.raises(TimeoutError):
        execute_code_review_workflow_sync(
            _review_input(), workflow_id="wid-3", execute_timeout_s=0.1
        )
