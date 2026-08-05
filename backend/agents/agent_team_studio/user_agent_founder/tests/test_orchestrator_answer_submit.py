"""Tests for the answer-submission retry policy in ``_answer_pending_questions``.

The submit loop retries transient failures (transport, 408/429, 5xx) with backoff
but must stop immediately on a non-retryable client error such as a 409 — which the
agentic-team pipeline now returns when a run is no longer resumable (timed out,
cancelled, or reaped). Without the short-circuit the persona would burn its whole
retry+backoff budget on a run that can never accept the answer.
"""

from __future__ import annotations

from typing import Any

import httpx

from agent_team_studio.user_agent_founder import orchestrator
from agent_team_studio.user_agent_founder.orchestrator import (
    ANSWER_POST_RETRIES,
    _answer_pending_questions,
)


class _FakeAgent:
    def answer_question(self, q: dict[str, Any]) -> dict[str, Any]:
        return {"selected_option_id": "other", "other_text": "an answer", "rationale": "because"}


class _FakeStore:
    def __init__(self) -> None:
        self.chat_messages: list[dict[str, Any]] = []
        self.decisions: list[dict[str, Any]] = []

    def add_decision(self, **fields: Any) -> None:
        self.decisions.append(fields)

    def add_chat_message(self, **fields: Any) -> None:
        self.chat_messages.append(fields)


def _http_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://svc/input")
    response = httpx.Response(code, request=request, text=f"HTTP {code}")
    return httpx.HTTPStatusError(f"status {code}", request=request, response=response)


def _questions() -> list[dict[str, Any]]:
    return [{"id": "q1", "question_text": "Why?"}]


def test_non_retryable_409_stops_immediately(monkeypatch) -> None:
    # Avoid real backoff sleeps.
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _submit(_answers: list[dict[str, Any]]) -> None:
        calls["n"] += 1
        raise _http_error(409)

    store = _FakeStore()
    result = _answer_pending_questions(_FakeAgent(), store, "run1", "job1", _questions(), _submit)

    assert result is False
    # 409 is terminal — submitted exactly once, no retry storm.
    assert calls["n"] == 1
    assert any("no longer resumable" in m["content"] for m in store.chat_messages)


def test_retryable_5xx_exhausts_attempts(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _submit(_answers: list[dict[str, Any]]) -> None:
        calls["n"] += 1
        raise _http_error(503)

    store = _FakeStore()
    result = _answer_pending_questions(_FakeAgent(), store, "run1", "job1", _questions(), _submit)

    assert result is False
    # 5xx is transient — retried up to the configured budget.
    assert calls["n"] == ANSWER_POST_RETRIES


def test_transient_404_is_retried(monkeypatch) -> None:
    """Only 409 is terminal; other 4xx (e.g. a transient 404 'not yet visible') must
    still exhaust the retry budget rather than short-circuiting."""
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _submit(_answers: list[dict[str, Any]]) -> None:
        calls["n"] += 1
        raise _http_error(404)

    store = _FakeStore()
    result = _answer_pending_questions(_FakeAgent(), store, "run1", "job1", _questions(), _submit)

    assert result is False
    assert calls["n"] == ANSWER_POST_RETRIES  # retried, not short-circuited
    # And it must NOT be mislabeled as "no longer resumable".
    assert not any("no longer resumable" in m.get("content", "") for m in store.chat_messages)


def test_success_returns_true(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def _submit(_answers: list[dict[str, Any]]) -> None:
        calls["n"] += 1  # succeeds on first try

    store = _FakeStore()
    result = _answer_pending_questions(_FakeAgent(), store, "run1", "job1", _questions(), _submit)

    assert result is True
    assert calls["n"] == 1
