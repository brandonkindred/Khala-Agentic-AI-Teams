"""Tests for call_llm_with_retries, focused on the on_retry notification hook."""

from __future__ import annotations

from typing import Any, List, Tuple

import pytest

from llm_service.interface import (
    LLMPermanentError,
    LLMTemporaryError,
    LLMUnreachableAfterRetriesError,
)
from llm_service.util import call_llm_with_retries


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backoff sleeps would slow the suite; the hook fires before the sleep anyway."""
    monkeypatch.setattr("llm_service.util.time.sleep", lambda _s: None)


def _recording_hook(calls: List[Tuple[int, int, float, Exception]]) -> Any:
    def _hook(attempt: int, max_attempts: int, wait: float, exc: Exception) -> None:
        calls.append((attempt, max_attempts, wait, exc))

    return _hook


def test_on_retry_called_once_per_retried_failure() -> None:
    """Two transient failures then success → on_retry fires exactly twice,
    with the failed attempt number, the configured max, and the failing exception."""
    calls: List[Tuple[int, int, float, Exception]] = []
    attempts = {"n": 0}

    def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LLMTemporaryError(f"boom {attempts['n']}")
        return "ok"

    result = call_llm_with_retries(_fn, max_attempts=3, on_retry=_recording_hook(calls))
    assert result == "ok"
    assert [(c[0], c[1]) for c in calls] == [(1, 3), (2, 3)]
    assert all(c[2] >= 0 for c in calls)
    assert all(isinstance(c[3], LLMTemporaryError) for c in calls)


def test_on_retry_not_called_on_immediate_success() -> None:
    calls: List[Tuple[int, int, float, Exception]] = []
    result = call_llm_with_retries(lambda: "ok", max_attempts=3, on_retry=_recording_hook(calls))
    assert result == "ok"
    assert calls == []


def test_on_retry_not_called_after_final_attempt() -> None:
    """All attempts fail → the final attempt raises without a notification,
    so on_retry fires max_attempts - 1 times."""
    calls: List[Tuple[int, int, float, Exception]] = []

    def _fn() -> None:
        raise LLMTemporaryError("always down")

    with pytest.raises(LLMUnreachableAfterRetriesError):
        call_llm_with_retries(_fn, max_attempts=3, on_retry=_recording_hook(calls))
    assert [(c[0], c[1]) for c in calls] == [(1, 3), (2, 3)]


def test_on_retry_not_called_on_permanent_error() -> None:
    """Permanent errors re-raise immediately — no retry, no notification."""
    calls: List[Tuple[int, int, float, Exception]] = []

    def _fn() -> None:
        raise LLMPermanentError("bad request")

    with pytest.raises(LLMPermanentError):
        call_llm_with_retries(_fn, max_attempts=3, on_retry=_recording_hook(calls))
    assert calls == []


def test_on_retry_called_for_unexpected_exception_branch() -> None:
    """The generic-Exception branch also notifies (e.g. a JSON shape error)."""
    calls: List[Tuple[int, int, float, Exception]] = []
    attempts = {"n": 0}

    def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("unexpected shape")
        return "ok"

    result = call_llm_with_retries(_fn, max_attempts=2, on_retry=_recording_hook(calls))
    assert result == "ok"
    assert len(calls) == 1
    assert isinstance(calls[0][3], ValueError)


def test_default_no_hook_unchanged() -> None:
    """on_retry omitted → behavior is exactly as before (retry then succeed)."""
    attempts = {"n": 0}

    def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise LLMTemporaryError("transient")
        return "ok"

    assert call_llm_with_retries(_fn, max_attempts=2) == "ok"


def test_raising_on_retry_hook_is_swallowed_and_retries_continue() -> None:
    """A raising on_retry hook is an observability bug; it must be logged and
    swallowed, never abort the remaining retries or replace the real error."""
    calls = {"fn": 0, "hook": 0}

    def fn():
        calls["fn"] += 1
        if calls["fn"] < 3:
            raise LLMTemporaryError("transient")
        return "ok"

    def bad_hook(n, m, wait, e):
        calls["hook"] += 1
        raise RuntimeError("hook bug")

    result = call_llm_with_retries(fn, max_attempts=3, on_retry=bad_hook)

    assert result == "ok"
    assert calls["fn"] == 3, "all attempts must run despite the raising hook"
    assert calls["hook"] == 2, "hook attempted once per retried failure"
