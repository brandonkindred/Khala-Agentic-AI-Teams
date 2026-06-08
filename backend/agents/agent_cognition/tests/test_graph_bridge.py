"""Tests for the sync→async bridge used by reflection grounding."""

from __future__ import annotations

import asyncio

from agent_cognition.graph.bridge import run_sync


def test_run_sync_returns_result_no_loop():
    async def _coro():
        return 42

    assert run_sync(_coro()) == 42


def test_run_sync_returns_default_on_exception():
    async def _boom():
        raise RuntimeError("nope")

    assert run_sync(_boom(), default="fallback") == "fallback"


def test_run_sync_returns_default_inside_running_loop():
    results = {}

    async def _outer():
        async def _inner():
            return "should-not-run"

        # Called from within a running loop → cannot block → default.
        results["v"] = run_sync(_inner(), default="defaulted")

    asyncio.run(_outer())
    assert results["v"] == "defaulted"
