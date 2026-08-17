"""Tests for ``llm_service.api.generate_structured``'s client-injection support.

Covers the ``llm_client`` parameter added so callers that already hold a
resolved client (e.g. ``software_engineering_team.shared.single_shot_review``)
can reuse it here instead of a second, independently-resolved client, plus
``think``/``context``/``**kwargs`` forwarding to ``complete_validated``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from llm_service import api as api_mod
from llm_service.api import generate_structured
from llm_service.interface import LLMClient


class _Answer(BaseModel):
    approved: bool
    summary: str


class _StubClient(LLMClient):
    """Minimal LLMClient stub — routes ``complete_json`` through a canned payload."""

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
        raise AssertionError("complete() should not be called by generate_structured")

    def get_max_context_tokens(self) -> int:
        return 8192


def test_uses_injected_llm_client_without_calling_get_client(monkeypatch):
    client = _StubClient({"approved": True, "summary": "ok"})
    calls: list[str | None] = []
    monkeypatch.setattr(
        api_mod, "get_client", lambda agent_key=None: calls.append(agent_key) or client
    )

    result = generate_structured(
        "prompt text",
        schema=_Answer,
        objective="test call",
        llm_client=client,
    )

    assert calls == []
    assert isinstance(result, _Answer)
    assert len(client.calls) == 1


def test_falls_back_to_get_client_when_no_llm_client_injected(monkeypatch):
    client = _StubClient({"approved": True, "summary": "ok"})
    calls: list[str | None] = []
    monkeypatch.setattr(
        api_mod, "get_client", lambda agent_key=None: calls.append(agent_key) or client
    )

    result = generate_structured(
        "prompt text",
        schema=_Answer,
        objective="test call",
        agent_key="devops",
    )

    assert calls == ["devops"]
    assert isinstance(result, _Answer)


def test_forwards_think_and_context_and_kwargs(monkeypatch):
    recorded = {}

    def fake_complete_validated(
        client_arg, prompt, *, schema, objective, system_prompt=None, **kwargs
    ):
        recorded.update(kwargs)
        return schema(approved=True, summary="ok")

    monkeypatch.setattr(api_mod, "complete_validated", fake_complete_validated)
    client = _StubClient({"approved": True, "summary": "ok"})
    ctx = {"allowed": {"a"}}

    generate_structured(
        "prompt text",
        schema=_Answer,
        objective="test call",
        llm_client=client,
        think=True,
        context=ctx,
        extra_flag="value",
    )

    assert recorded["think"] is True
    assert recorded["context"] is ctx
    assert recorded["extra_flag"] == "value"


def test_think_defaults_to_false(monkeypatch):
    recorded = {}

    def fake_complete_validated(
        client_arg, prompt, *, schema, objective, system_prompt=None, **kwargs
    ):
        recorded.update(kwargs)
        return schema(approved=True, summary="ok")

    monkeypatch.setattr(api_mod, "complete_validated", fake_complete_validated)
    client = _StubClient({"approved": True, "summary": "ok"})

    generate_structured("prompt text", schema=_Answer, objective="test call", llm_client=client)

    assert recorded["think"] is False
    assert recorded["context"] is None
