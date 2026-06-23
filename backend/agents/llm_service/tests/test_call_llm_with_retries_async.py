"""Tests for call_llm_with_retries_async — the non-blocking retry helper.

Mirrors test_call_llm_with_retries.py: same error-classification, backoff
schedule, and on_retry semantics, but awaiting fn() and asyncio.sleep so an
async caller never blocks the event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, List, Tuple

import pytest

from llm_service.interface import (
    LLMPermanentError,
    LLMTemporaryError,
    LLMUnreachableAfterRetriesError,
)
from llm_service.util import call_llm_with_retries_async


@pytest.fixture(autouse=True)
def _no_async_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace asyncio.sleep with an instant no-op coroutine so backoff waits
    don't slow the suite (the hook fires before the sleep anyway)."""

    async def _instant(_seconds: float) -> None:
        return None

    monkeypatch.setattr("llm_service.util.asyncio.sleep", _instant)


def _recording_hook(calls: List[Tuple[int, int, float, Exception]]) -> Any:
    def _hook(attempt: int, max_attempts: int, wait: float, exc: Exception) -> None:
        calls.append((attempt, max_attempts, wait, exc))

    return _hook


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_async_on_retry_called_once_per_retried_failure() -> None:
    calls: List[Tuple[int, int, float, Exception]] = []
    attempts = {"n": 0}

    async def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LLMTemporaryError(f"boom {attempts['n']}")
        return "ok"

    result = _run(call_llm_with_retries_async(_fn, max_attempts=3, on_retry=_recording_hook(calls)))
    assert result == "ok"
    assert [(c[0], c[1]) for c in calls] == [(1, 3), (2, 3)]
    assert all(c[2] >= 0 for c in calls)
    assert all(isinstance(c[3], LLMTemporaryError) for c in calls)


def test_async_on_retry_not_called_on_immediate_success() -> None:
    calls: List[Tuple[int, int, float, Exception]] = []

    async def _ok() -> str:
        return "ok"

    result = _run(call_llm_with_retries_async(_ok, max_attempts=3, on_retry=_recording_hook(calls)))
    assert result == "ok"
    assert calls == []


def test_async_exhaustion_raises_and_skips_final_notification() -> None:
    calls: List[Tuple[int, int, float, Exception]] = []

    async def _fn() -> None:
        raise LLMTemporaryError("always down")

    with pytest.raises(LLMUnreachableAfterRetriesError):
        _run(call_llm_with_retries_async(_fn, max_attempts=3, on_retry=_recording_hook(calls)))
    assert [(c[0], c[1]) for c in calls] == [(1, 3), (2, 3)]


def test_async_permanent_error_reraises_immediately() -> None:
    calls: List[Tuple[int, int, float, Exception]] = []

    async def _fn() -> None:
        raise LLMPermanentError("bad request")

    with pytest.raises(LLMPermanentError):
        _run(call_llm_with_retries_async(_fn, max_attempts=3, on_retry=_recording_hook(calls)))
    assert calls == []


def test_async_generic_exception_branch_retries() -> None:
    calls: List[Tuple[int, int, float, Exception]] = []
    attempts = {"n": 0}

    async def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("unexpected shape")
        return "ok"

    result = _run(call_llm_with_retries_async(_fn, max_attempts=2, on_retry=_recording_hook(calls)))
    assert result == "ok"
    assert len(calls) == 1
    assert isinstance(calls[0][3], ValueError)


def test_async_default_no_hook_unchanged() -> None:
    attempts = {"n": 0}

    async def _fn() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise LLMTemporaryError("transient")
        return "ok"

    assert _run(call_llm_with_retries_async(_fn, max_attempts=2)) == "ok"


def test_async_raising_on_retry_hook_is_swallowed() -> None:
    calls = {"fn": 0, "hook": 0}

    async def fn() -> str:
        calls["fn"] += 1
        if calls["fn"] < 3:
            raise LLMTemporaryError("transient")
        return "ok"

    def bad_hook(n, m, wait, e):
        calls["hook"] += 1
        raise RuntimeError("hook bug")

    result = _run(call_llm_with_retries_async(fn, max_attempts=3, on_retry=bad_hook))
    assert result == "ok"
    assert calls["fn"] == 3
    assert calls["hook"] == 2
