"""Tests for branding_team.shared.coro_runner.run_coroutine."""

from __future__ import annotations

import asyncio
import threading

import pytest

from branding_team.shared import coro_runner as coro_runner_mod
from branding_team.shared.coro_runner import run_coroutine
from shared.concurrency import LazySingleton


def test_run_coro_offloads_when_loop_running() -> None:
    """run_coroutine returns the coroutine result on a worker thread when a loop is already active."""

    async def _driver():
        loop_thread_ident = threading.current_thread().ident

        async def _val():
            return 42, threading.current_thread().ident

        # Called synchronously inside a running loop, so run_coroutine must offload
        # to a worker thread instead of calling asyncio.run on the live loop.
        return loop_thread_ident, run_coroutine(_val())

    loop_thread_ident, (value, worker_thread_ident) = asyncio.run(_driver())
    assert value == 42
    assert worker_thread_ident is not None
    assert worker_thread_ident != loop_thread_ident


def test_offload_pool_registers_atexit_shutdown_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_get_offload_pool's atexit shutdown registration fires exactly once, at first construction.

    Mirrors the singleton-swap idiom in ``tests/test_store_singleton.py``: swap in a
    fresh ``LazySingleton`` for the duration of this test, then restore whatever was
    cached before it so other tests in this process keep seeing their expected pool.
    """
    original = coro_runner_mod._offload_pool
    coro_runner_mod._offload_pool = LazySingleton()
    register_calls: list[tuple] = []
    monkeypatch.setattr(
        coro_runner_mod.atexit, "register", lambda *a, **kw: register_calls.append((a, kw))
    )

    try:
        pool_first = coro_runner_mod._get_offload_pool()
        pool_second = coro_runner_mod._get_offload_pool()
        assert pool_first is pool_second
        assert len(register_calls) == 1
    finally:
        pool_first.shutdown(wait=False)
        coro_runner_mod._offload_pool = original
