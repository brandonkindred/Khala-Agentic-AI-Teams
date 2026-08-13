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
        "agent_platform.sandbox.temporal.dispatch.start_sandbox_reaper_workflow",
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
        "agent_platform.sandbox.temporal.dispatch.start_sandbox_reaper_workflow",
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
        "agent_platform.sandbox.temporal.dispatch.start_sandbox_reaper_workflow",
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
        "agent_platform.sandbox.temporal.dispatch.start_sandbox_reaper_workflow",
        _start,
    )

    with pytest.raises(asyncio.CancelledError):
        await main._start_sandbox_reaper_with_retry()


@pytest.mark.asyncio
async def test_start_sandbox_reaper_task_boots_sandbox_worker_when_temporal_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 regression: when Temporal is enabled, this process must boot its
    own sandbox-only Temporal worker BEFORE starting the reaper workflow —
    the reaper workflow (and every other sandbox activity) only ever
    executes correctly if a worker polling SANDBOX_TASK_QUEUE is running
    inside this same process."""
    monkeypatch.setattr(
        "agent_platform.sandbox.temporal.dispatch.sandbox_temporal_enabled",
        lambda: True,
    )
    worker_started = []
    monkeypatch.setattr(
        "agent_platform.sandbox.temporal.worker.start_agent_platform_sandbox_temporal_worker_thread",
        lambda: worker_started.append(True),
    )

    reaper_started = []

    async def fake_retry() -> None:
        reaper_started.append(True)

    monkeypatch.setattr(main, "_start_sandbox_reaper_with_retry", fake_retry)

    task = await main._start_sandbox_reaper_task()
    await task

    assert worker_started == [True]
    assert reaper_started == [True]


@pytest.mark.asyncio
async def test_start_sandbox_reaper_task_uses_in_process_reaper_when_temporal_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Temporal is disabled, must fall back to the in-process asyncio
    reaper task and must NOT boot the sandbox Temporal worker."""
    monkeypatch.setattr(
        "agent_platform.sandbox.temporal.dispatch.sandbox_temporal_enabled",
        lambda: False,
    )
    worker_started = []
    monkeypatch.setattr(
        "agent_platform.sandbox.temporal.worker.start_agent_platform_sandbox_temporal_worker_thread",
        lambda: worker_started.append(True),
    )

    reaper_started = []

    async def fake_run_idle_reaper(*, interval_s: int = 60) -> None:
        reaper_started.append(True)

    monkeypatch.setattr(
        "agent_platform.sandbox.run_idle_reaper",
        fake_run_idle_reaper,
    )

    task = await main._start_sandbox_reaper_task()
    await task

    assert worker_started == []
    assert reaper_started == [True]


@pytest.mark.asyncio
async def test_maybe_start_sandbox_reaper_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion: UNIFIED_API_SANDBOX_TEMPORAL_WORKER=false must skip
    starting the sandbox reaper (and therefore the sandbox Temporal worker
    thread it would otherwise boot) entirely."""
    monkeypatch.setattr(main, "UNIFIED_API_SANDBOX_TEMPORAL_WORKER", False)
    called: list[bool] = []

    async def fake_start() -> asyncio.Task:
        called.append(True)
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(main, "_start_sandbox_reaper_task", fake_start)

    result = await main._maybe_start_sandbox_reaper()

    assert called == []
    assert result is None


@pytest.mark.asyncio
async def test_maybe_start_sandbox_reaper_starts_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default (flag true, or unset) must preserve today's always-on behavior."""
    monkeypatch.setattr(main, "UNIFIED_API_SANDBOX_TEMPORAL_WORKER", True)
    called: list[bool] = []

    async def fake_start() -> asyncio.Task:
        called.append(True)
        return asyncio.create_task(asyncio.sleep(0))

    monkeypatch.setattr(main, "_start_sandbox_reaper_task", fake_start)

    result = await main._maybe_start_sandbox_reaper()

    assert called == [True]
    assert isinstance(result, asyncio.Task)
    await result


@pytest.mark.asyncio
async def test_maybe_start_sandbox_reaper_swallows_startup_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure inside _start_sandbox_reaper_task must be logged and swallowed,
    not propagated — startup of the rest of the app must not be aborted."""
    monkeypatch.setattr(main, "UNIFIED_API_SANDBOX_TEMPORAL_WORKER", True)

    async def fake_start() -> asyncio.Task:
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_start_sandbox_reaper_task", fake_start)

    result = await main._maybe_start_sandbox_reaper()

    assert result is None
