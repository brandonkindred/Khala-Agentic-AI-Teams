"""Tests for ``shared.single_shot_review.run_single_shot_review``.

Covers client resolution (injected vs. ``get_client(agent_key)``), the
schema-validated mode (delegates to ``complete_validated``) vs. the
plain-JSON mode (delegates to ``client.complete_json`` directly), objective
defaulting, and the empty-``agent_key``/``prompt`` preconditions.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from llm_service import LLMClient
from software_engineering_team.shared import single_shot_review as mod
from software_engineering_team.shared.single_shot_review import run_single_shot_review


class _Result(BaseModel):
    approved: bool
    summary: str


class _StubClient(LLMClient):
    """Minimal LLMClient stub recording every ``complete_json`` call."""

    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        prompt,
        *,
        objective,
        temperature=0.0,
        system_prompt=None,
        tools=None,
        think=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "objective": objective,
                "temperature": temperature,
                "system_prompt": system_prompt,
                "think": think,
                "kwargs": kwargs,
            }
        )
        return self._payload

    def complete(
        self, prompt, *, objective, temperature=0.7, max_tokens=None, system_prompt=None, **kwargs
    ):
        raise AssertionError("complete() should not be called by run_single_shot_review")

    def get_max_context_tokens(self) -> int:
        return 8192


def _patch_get_client(monkeypatch, client: _StubClient) -> list[str]:
    calls: list[str] = []

    def fake_get_client(agent_key):
        calls.append(agent_key)
        return client

    monkeypatch.setattr(mod, "get_client", fake_get_client)
    return calls


# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------


def test_empty_agent_key_raises():
    client = _StubClient({"approved": True, "summary": "ok"})
    with pytest.raises(AssertionError):
        run_single_shot_review(client, "", "prompt text")


def test_empty_prompt_raises():
    client = _StubClient({"approved": True, "summary": "ok"})
    with pytest.raises(AssertionError):
        run_single_shot_review(client, "devops", "   ")


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------


def test_uses_injected_client_without_calling_get_client(monkeypatch):
    client = _StubClient({"approved": True, "summary": "ok"})
    calls = _patch_get_client(monkeypatch, client)

    run_single_shot_review(client, "devops", "prompt text")

    assert calls == []
    assert len(client.calls) == 1


def test_resolves_client_via_get_client_when_none(monkeypatch):
    client = _StubClient({"approved": True, "summary": "ok"})
    calls = _patch_get_client(monkeypatch, client)

    run_single_shot_review(None, "devops", "prompt text")

    assert calls == ["devops"]
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# Plain-JSON mode (schema=None)
# ---------------------------------------------------------------------------


def test_plain_json_mode_returns_raw_dict():
    client = _StubClient({"approved": False, "summary": "found issues", "extra": 1})

    result = run_single_shot_review(client, "devops", "prompt text", "sys prompt")

    assert result == {"approved": False, "summary": "found issues", "extra": 1}
    call = client.calls[0]
    assert call["prompt"] == "prompt text"
    assert call["system_prompt"] == "sys prompt"
    assert call["objective"] == "devops single-shot review"
    assert call["think"] is False
    assert call["temperature"] == 0.0


def test_plain_json_mode_forwards_kwargs_and_overrides():
    client = _StubClient({"approved": True, "summary": "ok"})

    run_single_shot_review(
        client,
        "devops",
        "prompt text",
        objective="custom objective",
        temperature=0.5,
        think=True,
        extra_flag="value",
    )

    call = client.calls[0]
    assert call["objective"] == "custom objective"
    assert call["temperature"] == 0.5
    assert call["think"] is True
    assert call["kwargs"] == {"extra_flag": "value"}


# ---------------------------------------------------------------------------
# Schema-validated mode
# ---------------------------------------------------------------------------


def test_schema_mode_returns_validated_instance():
    client = _StubClient({"approved": True, "summary": "clean"})

    result = run_single_shot_review(client, "devops", "prompt text", schema=_Result)

    assert isinstance(result, _Result)
    assert result.approved is True
    assert result.summary == "clean"


def test_schema_mode_does_not_call_complete_json_directly(monkeypatch):
    client = _StubClient({"approved": True, "summary": "clean"})
    recorded = {}

    def fake_complete_validated(
        client_arg, prompt, *, schema, objective, system_prompt=None, **kwargs
    ):
        recorded["client"] = client_arg
        recorded["prompt"] = prompt
        recorded["schema"] = schema
        recorded["objective"] = objective
        recorded["system_prompt"] = system_prompt
        recorded["kwargs"] = kwargs
        return schema(approved=True, summary="clean")

    monkeypatch.setattr(mod, "complete_validated", fake_complete_validated)

    result = run_single_shot_review(client, "devops", "prompt text", "sys prompt", schema=_Result)

    assert client.calls == []
    assert isinstance(result, _Result)
    assert recorded["client"] is client
    assert recorded["schema"] is _Result
    assert recorded["objective"] == "devops single-shot review"
    assert recorded["system_prompt"] == "sys prompt"
    assert recorded["kwargs"]["temperature"] == 0.0
    assert recorded["kwargs"]["correction_attempts"] == 1
    assert recorded["kwargs"]["think"] is False


def test_schema_mode_forwards_correction_attempts_and_context(monkeypatch):
    client = _StubClient({"approved": True, "summary": "clean"})
    recorded = {}

    def fake_complete_validated(
        client_arg, prompt, *, schema, objective, system_prompt=None, **kwargs
    ):
        recorded.update(kwargs)
        return schema(approved=True, summary="clean")

    monkeypatch.setattr(mod, "complete_validated", fake_complete_validated)

    ctx = {"allowed": {"a"}}
    run_single_shot_review(
        client,
        "devops",
        "prompt text",
        schema=_Result,
        correction_attempts=3,
        context=ctx,
    )

    assert recorded["correction_attempts"] == 3
    assert recorded["context"] is ctx
