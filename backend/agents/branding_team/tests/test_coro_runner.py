"""Tests for branding_team.shared.coro_runner.run_coroutine."""

from __future__ import annotations

import asyncio
import threading

from branding_team.shared.coro_runner import run_coroutine


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
