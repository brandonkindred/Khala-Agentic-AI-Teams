"""Tests for branding_team.shared.coro_runner.run_coroutine."""

from __future__ import annotations

import asyncio

from branding_team.shared.coro_runner import run_coroutine


def test_run_coro_offloads_when_loop_running() -> None:
    """run_coroutine runs a coroutine on a worker thread when a loop is already active."""

    async def _driver():
        async def _val():
            return 42

        # Called synchronously inside a running loop, so run_coroutine must offload
        # to a worker thread instead of calling asyncio.run on the live loop.
        return run_coroutine(_val())

    assert asyncio.run(_driver()) == 42
