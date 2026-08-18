"""Tests for the unified-API lifespan shutdown helper.

``lifespan`` cancels several background tasks after ``yield`` and must await
each one so its cleanup finishes before the process exits — see
``_cancel_and_await_task``. The full ``lifespan`` context manager is not
driven here (it requires live Postgres/Temporal/Neo4j — see its
``# pragma: no cover``); these tests exercise the extracted helper directly.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import unified_api.main as main


@pytest.mark.asyncio
async def test_cancel_and_await_task_none_is_noop() -> None:
    """A ``None`` task (e.g. a worker that never started) must be a no-op."""
    await main._cancel_and_await_task(None, label="unused")


@pytest.mark.asyncio
async def test_cancel_and_await_task_suppresses_cancelled_error() -> None:
    """Cancelling a well-behaved task must be awaited to completion, and the
    resulting CancelledError must not propagate out of the helper."""
    task = asyncio.create_task(asyncio.sleep(10))
    await asyncio.sleep(0)  # let the task actually start and reach the sleep

    await main._cancel_and_await_task(task, label="sleepy-task")

    assert task.done()
    assert task.cancelled()


@pytest.mark.asyncio
async def test_cancel_and_await_task_logs_non_cancelled_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A task whose own cancellation handling raises something other than
    CancelledError must be caught and logged, not left to propagate and
    abort the rest of shutdown."""

    async def _boom() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise RuntimeError("boom during cancellation") from None

    task = asyncio.create_task(_boom())
    await asyncio.sleep(0)  # let the task start and reach the inner await

    with caplog.at_level(logging.WARNING, logger="unified_api"):
        await main._cancel_and_await_task(task, label="boom-task")

    assert task.done()
    assert any("boom-task" in record.message for record in caplog.records)
    assert caplog.records[-1].exc_info is not None


@pytest.mark.asyncio
async def test_cancel_and_await_task_is_a_noop_on_an_already_done_task() -> None:
    """Calling the helper on a task that already finished normally must not
    raise (``task.cancel()`` on a done task is a safe no-op, and awaiting an
    already-finished task just returns its result)."""
    task = asyncio.create_task(asyncio.sleep(0))
    await task

    await main._cancel_and_await_task(task, label="already-done-task")

    assert task.done()
