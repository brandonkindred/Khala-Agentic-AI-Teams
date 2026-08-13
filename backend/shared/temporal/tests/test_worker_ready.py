"""Tests for shared Temporal worker readiness helpers."""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock

import pytest

import shared.temporal.worker as worker


@pytest.fixture(autouse=True)
def _clean_worker_registry():
    worker._worker_threads.clear()
    worker._worker_ready.clear()
    yield
    worker._worker_threads.clear()
    worker._worker_ready.clear()


def test_is_team_worker_alive_false_when_unregistered():
    assert worker.is_team_worker_alive("missing-team") is False


def test_is_team_worker_alive_true_when_thread_alive():
    event = threading.Event()

    def _target():
        event.wait(timeout=2)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    worker._worker_threads["alive-team"] = thread
    try:
        assert worker.is_team_worker_alive("alive-team") is True
    finally:
        event.set()
        thread.join(timeout=2)


def test_is_team_worker_ready_false_when_unregistered():
    assert worker.is_team_worker_ready("missing-team") is False


def test_is_team_worker_ready_false_when_alive_but_not_connected():
    """Thread spawned, connect still in flight — ready event unset."""
    hold = threading.Event()

    def _target():
        hold.wait(timeout=2)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    ready = threading.Event()
    worker._worker_threads["connecting-team"] = thread
    worker._worker_ready["connecting-team"] = ready
    try:
        assert worker.is_team_worker_alive("connecting-team") is True
        assert worker.is_team_worker_ready("connecting-team") is False
    finally:
        hold.set()
        thread.join(timeout=2)


def test_is_team_worker_ready_true_when_alive_and_connected():
    hold = threading.Event()

    def _target():
        hold.wait(timeout=2)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    ready = threading.Event()
    ready.set()
    worker._worker_threads["ready-team"] = thread
    worker._worker_ready["ready-team"] = ready
    try:
        assert worker.is_team_worker_ready("ready-team") is True
    finally:
        hold.set()
        thread.join(timeout=2)


def test_is_team_worker_ready_false_when_event_set_but_thread_dead():
    ready = threading.Event()
    ready.set()
    worker._worker_ready["ghost-team"] = ready
    assert worker.is_team_worker_ready("ghost-team") is False


def test_wait_for_team_worker_ready_raises_when_not_started():
    with pytest.raises(RuntimeError, match="was not started"):
        worker.wait_for_team_worker_ready("no-such-team", timeout_s=0.05)


def test_wait_for_team_worker_ready_returns_when_event_set_and_alive():
    ready = threading.Event()
    hold = threading.Event()

    def _target():
        hold.wait(timeout=2)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    worker._worker_threads["ready-team"] = thread
    worker._worker_ready["ready-team"] = ready
    ready.set()
    try:
        worker.wait_for_team_worker_ready("ready-team", timeout_s=0.5)
    finally:
        hold.set()
        thread.join(timeout=2)


def test_wait_for_team_worker_ready_raises_exited_after_start_on_timeout():
    ready = threading.Event()
    worker._worker_ready["dead-team"] = ready
    # No live thread → timeout path reports exited after start.

    with pytest.raises(RuntimeError, match="exited after start"):
        worker.wait_for_team_worker_ready("dead-team", timeout_s=0.05)


def test_wait_for_team_worker_ready_raises_exited_when_event_set_but_thread_dead():
    ready = threading.Event()
    ready.set()
    worker._worker_ready["ghost-team"] = ready
    # Event set (connect happened) but thread already gone.

    with pytest.raises(RuntimeError, match="exited after start"):
        worker.wait_for_team_worker_ready("ghost-team", timeout_s=0.05)


def test_wait_for_team_worker_ready_raises_never_became_ready_while_thread_alive():
    ready = threading.Event()
    hold = threading.Event()

    def _target():
        hold.wait(timeout=2)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    worker._worker_threads["slow-team"] = thread
    worker._worker_ready["slow-team"] = ready
    try:
        with pytest.raises(RuntimeError, match="never became ready"):
            worker.wait_for_team_worker_ready("slow-team", timeout_s=0.05)
    finally:
        hold.set()
        thread.join(timeout=2)


@pytest.mark.asyncio
async def test_run_worker_async_sets_ready_event_after_connect(monkeypatch):
    ready = threading.Event()
    worker._worker_ready["async-team"] = ready

    fake_client = object()
    monkeypatch.setattr(
        worker,
        "connect_temporal_client",
        AsyncMock(return_value=fake_client),
    )
    monkeypatch.setattr(worker, "set_temporal_client", lambda _c: None)
    monkeypatch.setattr(worker, "set_temporal_loop", lambda _l: None)
    monkeypatch.setattr(worker, "_build_workflow_runner", lambda: object())

    class _FakeWorker:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            assert ready.is_set(), "ready must be set before Worker.run"
            return None

    monkeypatch.setattr("temporalio.worker.Worker", _FakeWorker)

    await worker._run_worker_async(
        "async-team",
        "q",
        workflows=[],
        activities=[],
        max_concurrent_activities=1,
    )

    assert ready.is_set()
