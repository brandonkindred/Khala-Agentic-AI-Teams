"""Tests for stopping in-process Temporal workers on graceful shutdown."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import shared.temporal.worker as worker


@pytest.fixture(autouse=True)
def _clean_worker_registry():
    worker._worker_threads.clear()
    worker._worker_ready.clear()
    worker._workers.clear()
    worker._worker_loops.clear()
    worker._activity_executors.clear()
    yield
    worker._worker_threads.clear()
    worker._worker_ready.clear()
    worker._workers.clear()
    worker._worker_loops.clear()
    worker._activity_executors.clear()


def test_stop_all_team_workers_is_noop_when_none_registered() -> None:
    worker.stop_all_team_workers()


def test_stop_team_worker_unknown_team_is_noop() -> None:
    worker.stop_team_worker("missing-team")


def test_stop_all_team_workers_shuts_down_and_joins() -> None:
    """Worker.shutdown() must run on the worker loop, then the thread must exit."""
    shutdown_calls: list[str] = []
    run_released = threading.Event()

    class FakeWorker:
        async def shutdown(self) -> None:
            shutdown_calls.append("shutdown")
            run_released.set()

    loop_ready = threading.Event()

    async def _run() -> None:
        worker._workers["studio"] = FakeWorker()
        worker._worker_loops["studio"] = asyncio.get_running_loop()
        loop_ready.set()
        await asyncio.get_running_loop().run_in_executor(None, run_released.wait)

    def _target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    thread = threading.Thread(target=_target, name="studio-temporal-worker", daemon=True)
    thread.start()
    worker._worker_threads["studio"] = thread
    worker._worker_ready["studio"] = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-stop-test")
    worker._activity_executors["studio"] = executor
    assert loop_ready.wait(timeout=2)

    worker.stop_all_team_workers(timeout_s=2.0)

    assert shutdown_calls == ["shutdown"]
    assert not thread.is_alive()
    assert "studio" not in worker._worker_threads
    assert "studio" not in worker._workers
    assert "studio" not in worker._worker_loops
    assert "studio" not in worker._worker_ready
    assert "studio" not in worker._activity_executors


def test_stop_team_worker_joins_dead_thread_without_shutdown() -> None:
    thread = threading.Thread(target=lambda: None, daemon=True)
    thread.start()
    thread.join(timeout=1)
    worker._worker_threads["gone"] = thread

    worker.stop_team_worker("gone")

    assert "gone" not in worker._worker_threads


def test_stop_team_worker_keeps_registry_when_thread_outlives_timeout() -> None:
    """A still-running worker must stay registered so a later join can wait it out."""
    hold = threading.Event()

    class NoopShutdown:
        async def shutdown(self) -> None:
            return

    loop_ready = threading.Event()

    async def _run() -> None:
        worker._workers["stuck"] = NoopShutdown()
        worker._worker_loops["stuck"] = asyncio.get_running_loop()
        loop_ready.set()
        await asyncio.get_running_loop().run_in_executor(None, hold.wait)

    def _target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    thread = threading.Thread(target=_target, name="stuck-temporal-worker", daemon=True)
    thread.start()
    worker._worker_threads["stuck"] = thread
    worker._activity_executors["stuck"] = ThreadPoolExecutor(max_workers=1)
    assert loop_ready.wait(timeout=2)

    try:
        still_stopped = worker.stop_team_worker("stuck", timeout_s=0.05)
        assert still_stopped is False
        assert thread.is_alive()
        assert worker._worker_threads["stuck"] is thread
        assert "stuck" in worker._workers
        assert "stuck" in worker._activity_executors
    finally:
        hold.set()
        thread.join(timeout=2)


def test_stop_all_team_workers_waits_until_thread_exits() -> None:
    """Storage teardown callers wait out an in-flight activity that missed timeout_s."""
    hold = threading.Event()

    class NoopShutdown:
        async def shutdown(self) -> None:
            return

    loop_ready = threading.Event()

    async def _run() -> None:
        worker._workers["slow"] = NoopShutdown()
        worker._worker_loops["slow"] = asyncio.get_running_loop()
        loop_ready.set()
        await asyncio.get_running_loop().run_in_executor(None, hold.wait)

    def _target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    thread = threading.Thread(target=_target, name="slow-temporal-worker", daemon=True)
    thread.start()
    worker._worker_threads["slow"] = thread
    assert loop_ready.wait(timeout=2)

    def _release() -> None:
        time.sleep(0.2)
        hold.set()

    threading.Thread(target=_release, daemon=True).start()
    worker.stop_all_team_workers(timeout_s=0.05)

    assert not thread.is_alive()
    assert "slow" not in worker._worker_threads
    assert "slow" not in worker._workers


def test_stop_all_team_workers_swallows_shutdown_errors() -> None:
    class BoomWorker:
        async def shutdown(self) -> None:
            raise RuntimeError("shutdown exploded")

    loop_ready = threading.Event()
    stop_loop = threading.Event()

    async def _run() -> None:
        worker._workers["boom"] = BoomWorker()
        worker._worker_loops["boom"] = asyncio.get_running_loop()
        loop_ready.set()
        await asyncio.get_running_loop().run_in_executor(None, stop_loop.wait)

    def _target() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_run())
        finally:
            loop.close()

    thread = threading.Thread(target=_target, name="boom-temporal-worker", daemon=True)
    thread.start()
    worker._worker_threads["boom"] = thread
    assert loop_ready.wait(timeout=2)

    try:
        worker.stop_team_worker("boom", timeout_s=0.2)
        assert "boom" in worker._worker_threads
    finally:
        stop_loop.set()
        thread.join(timeout=2)
