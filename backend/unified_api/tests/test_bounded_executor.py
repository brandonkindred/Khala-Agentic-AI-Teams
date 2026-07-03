"""Tests for the shared bounded-executor helpers (get_or_recreate_executor, submit_safely)."""

import logging
import sys
from concurrent import futures
from pathlib import Path
from unittest.mock import MagicMock

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from unified_api.bounded_executor import get_or_recreate_executor, submit_safely  # noqa: E402


def test_get_or_recreate_executor_creates_when_none():
    ex = get_or_recreate_executor(None, max_workers=2, thread_name_prefix="t")
    try:
        assert isinstance(ex, futures.ThreadPoolExecutor)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


def test_get_or_recreate_executor_reuses_live_instance():
    first = get_or_recreate_executor(None, max_workers=2, thread_name_prefix="t")
    try:
        second = get_or_recreate_executor(first, max_workers=2, thread_name_prefix="t")
        assert second is first
    finally:
        first.shutdown(wait=False, cancel_futures=True)


def test_get_or_recreate_executor_recreates_after_shutdown():
    first = get_or_recreate_executor(None, max_workers=2, thread_name_prefix="t")
    first.shutdown(wait=False, cancel_futures=True)
    second = get_or_recreate_executor(first, max_workers=2, thread_name_prefix="t")
    try:
        assert second is not first
    finally:
        second.shutdown(wait=False, cancel_futures=True)


class _NoShutdownAttr:
    """Stand-in for a ThreadPoolExecutor whose `_shutdown` attribute is absent —
    simulates a future CPython release removing/renaming that private attribute."""


def test_get_or_recreate_executor_degrades_gracefully_without_shutdown_attr():
    """If `_shutdown` ever disappears from ThreadPoolExecutor, getattr's default (False)
    means this function treats the object as still live rather than raising an
    AttributeError — the documented graceful-degradation behavior."""
    fake = _NoShutdownAttr()
    result = get_or_recreate_executor(fake, max_workers=2, thread_name_prefix="t")
    assert result is fake  # treated as "still live", not recreated; never raises


def test_submit_safely_calls_submit_with_args_and_returns_true():
    fake_executor = MagicMock()
    fn = MagicMock()
    accepted = submit_safely(fake_executor, fn, "a", "b", logger=logging.getLogger("test"), log_prefix="test")
    fake_executor.submit.assert_called_once_with(fn, "a", "b")
    assert accepted is True


def test_submit_safely_swallows_runtime_error_from_shutdown_executor_and_returns_false():
    """A shut-down executor's submit() raises RuntimeError; submit_safely must not
    propagate it (callers with a 'never raises' contract must keep that guarantee) and
    must return False so callers can roll back bookkeeping for work that never ran."""
    real_executor = futures.ThreadPoolExecutor(max_workers=1)
    real_executor.shutdown(wait=True)
    fn = MagicMock()

    accepted = submit_safely(real_executor, fn, logger=logging.getLogger("test"), log_prefix="test")

    fn.assert_not_called()
    assert accepted is False


def test_submit_safely_logs_on_runtime_error():
    fake_executor = MagicMock()
    fake_executor.submit.side_effect = RuntimeError("cannot schedule new futures after shutdown")
    fake_logger = MagicMock()

    submit_safely(fake_executor, MagicMock(), logger=fake_logger, log_prefix="my-integration")

    fake_logger.warning.assert_called_once()
    assert "my-integration" in fake_logger.warning.call_args[0]


class _BrokenPool:
    """Stand-in for a BrokenThreadPool: not shut down, but broken (a worker failed to
    spawn). Its submit() raises like the real thing."""

    _shutdown = False
    _broken = "a thread initializer failed"

    def submit(self, fn, *args):  # pragma: no cover - not exercised (recreated first)
        raise RuntimeError("cannot schedule new futures after a thread failed to start")


def test_get_or_recreate_executor_recreates_a_broken_pool():
    """A BrokenThreadPool (_broken set, _shutdown False) must be recreated, not reused —
    otherwise every submit() would raise and submit_safely would silently drop all work."""
    broken = _BrokenPool()
    result = get_or_recreate_executor(broken, max_workers=2, thread_name_prefix="t")
    try:
        assert result is not broken
        assert isinstance(result, futures.ThreadPoolExecutor)
    finally:
        result.shutdown(wait=False, cancel_futures=True)


def test_submit_safely_logs_exception_escaping_submitted_fn():
    """An exception that escapes the submitted fn is logged via the done-callback (a real
    ThreadPoolExecutor otherwise stores it on the discarded Future and never surfaces it)."""
    executor = futures.ThreadPoolExecutor(max_workers=1)
    fake_logger = MagicMock()

    def _boom():
        raise ValueError("work blew up")

    try:
        submit_safely(executor, _boom, logger=fake_logger, log_prefix="my-integration")
        executor.shutdown(wait=True)  # ensures the future completes and the callback runs
    finally:
        executor.shutdown(wait=True)

    fake_logger.error.assert_called_once()
    assert "my-integration" in fake_logger.error.call_args[0]


def test_submit_safely_no_error_log_on_clean_completion():
    executor = futures.ThreadPoolExecutor(max_workers=1)
    fake_logger = MagicMock()
    try:
        submit_safely(executor, lambda: 42, logger=fake_logger, log_prefix="ok")
        executor.shutdown(wait=True)
    finally:
        executor.shutdown(wait=True)
    fake_logger.error.assert_not_called()


def test_submit_safely_ignores_cancelled_future():
    """A cancelled future never ran the work, so the done-callback must return early
    without calling .exception() (which raises CancelledError on a cancelled future).
    Feeding an already-cancelled future to a mock executor makes add_done_callback fire
    the callback synchronously, exercising the `if fut.cancelled(): return` branch."""
    cancelled = futures.Future()
    assert cancelled.cancel() is True  # PENDING -> CANCELLED
    fake_executor = MagicMock()
    fake_executor.submit.return_value = cancelled
    fake_logger = MagicMock()

    submit_safely(fake_executor, MagicMock(), logger=fake_logger, log_prefix="x")  # must not raise

    fake_logger.error.assert_not_called()
