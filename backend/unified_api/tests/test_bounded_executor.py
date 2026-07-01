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


def test_submit_safely_calls_submit_with_args():
    fake_executor = MagicMock()
    fn = MagicMock()
    submit_safely(fake_executor, fn, "a", "b", logger=logging.getLogger("test"), log_prefix="test")
    fake_executor.submit.assert_called_once_with(fn, "a", "b")


def test_submit_safely_swallows_runtime_error_from_shutdown_executor():
    """A shut-down executor's submit() raises RuntimeError; submit_safely must not
    propagate it (callers with a 'never raises' contract must keep that guarantee)."""
    real_executor = futures.ThreadPoolExecutor(max_workers=1)
    real_executor.shutdown(wait=True)
    fn = MagicMock()

    submit_safely(real_executor, fn, logger=logging.getLogger("test"), log_prefix="test")  # must not raise

    fn.assert_not_called()


def test_submit_safely_logs_on_runtime_error():
    fake_executor = MagicMock()
    fake_executor.submit.side_effect = RuntimeError("cannot schedule new futures after shutdown")
    fake_logger = MagicMock()

    submit_safely(fake_executor, MagicMock(), logger=fake_logger, log_prefix="my-integration")

    fake_logger.warning.assert_called_once()
    assert "my-integration" in fake_logger.warning.call_args[0]
