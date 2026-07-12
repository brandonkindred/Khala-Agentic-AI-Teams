"""Tests for the unified-API lifespan hook that starts the sandbox idle reaper.

The Temporal-mode reaper startup (``_start_sandbox_reaper_with_retry``) must
never let a transient failure (e.g. the Temporal worker's client still
connecting) leave the reaper permanently unstarted — it retries with backoff
as a background task instead of a single blocking lifespan step. Regression
coverage for that fix.
"""

from __future__ import annotations

import asyncio

import pytest

import unified_api.main as main


@pytest.mark.asyncio
async def test_reaper_retry_succeeds_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def _start(**kwargs) -> None:
        calls.append(True)

    monkeypatch.setattr(
        "agent_provisioning_team.temporal.sandbox_dispatch.start_sandbox_reaper_workflow",
        _start,
    )
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await main._start_sandbox_reaper_with_retry()

    assert calls == [True]
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_reaper_retry_recovers_after_transient_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failures (e.g. the Temporal client not ready yet) then success —
    the reaper must still end up started, not permanently abandoned. Regression
    guard: the old code was a single blocking attempt, caught by an outer
    except and never retried, so the reaper could silently never start."""
    attempts: list[int] = []

    def _start(**kwargs) -> None:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr(
        "agent_provisioning_team.temporal.sandbox_dispatch.start_sandbox_reaper_workflow",
        _start,
    )
    sleep_calls: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr(main.asyncio, "sleep", fake_sleep)

    await main._start_sandbox_reaper_with_retry()

    assert len(attempts) == 3
    # Exponential backoff before each of the two failed attempts' retries.
    assert sleep_calls == [2.0, 4.0]


@pytest.mark.asyncio
async def test_reaper_retry_propagates_cancellation_from_backoff_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the background task (app shutdown) while it's backing off
    between retries must not be swallowed as just another retryable failure."""

    def _start(**kwargs) -> None:
        raise RuntimeError("still not ready")

    monkeypatch.setattr(
        "agent_provisioning_team.temporal.sandbox_dispatch.start_sandbox_reaper_workflow",
        _start,
    )

    async def cancelling_sleep(delay: float) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(main.asyncio, "sleep", cancelling_sleep)

    with pytest.raises(asyncio.CancelledError):
        await main._start_sandbox_reaper_with_retry()


@pytest.mark.asyncio
async def test_reaper_retry_propagates_cancellation_from_start_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the background task while the start attempt itself is
    in-flight (the realistic app-shutdown-during-startup case) must propagate,
    not be caught by the generic ``except Exception`` retry branch."""

    def _start(**kwargs) -> None:
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "agent_provisioning_team.temporal.sandbox_dispatch.start_sandbox_reaper_workflow",
        _start,
    )

    with pytest.raises(asyncio.CancelledError):
        await main._start_sandbox_reaper_with_retry()
