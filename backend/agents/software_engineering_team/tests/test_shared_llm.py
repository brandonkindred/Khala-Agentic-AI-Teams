"""Unit tests for software_engineering_team.shared.llm.complete_json_with_continuation.

Covers the markdown-fence/prose-prefix/trailing-comma recovery path, the
client-resolution fix (a raw LLMClient must not be silently discarded), and
system_prompt/think forwarding to the underlying Strands Agent invocation.
"""

from __future__ import annotations

import json

import pytest

from llm_service import LLMClient, LLMJsonParseError
from software_engineering_team.shared import llm as llm_mod
from software_engineering_team.shared.llm import complete_json_with_continuation


class _FakeAgentInstance:
    """Records the prompt/kwargs it was called with and returns canned text."""

    def __init__(self, text: str):
        self._text = text
        self.calls = []

    def __call__(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        return self._text


class _RecordingAgentFactory:
    """Stand-in for the strands.Agent class: records constructor kwargs and
    hands back a _FakeAgentInstance whose __call__ returns ``text``."""

    def __init__(self, text: str):
        self._text = text
        self.constructed_with = None
        self.instance = None

    def __call__(self, *, model, system_prompt, callback_handler=None):
        self.constructed_with = {"model": model, "system_prompt": system_prompt}
        self.instance = _FakeAgentInstance(self._text)
        return self.instance


def _patch_agent(monkeypatch, text: str) -> _RecordingAgentFactory:
    factory = _RecordingAgentFactory(text)
    monkeypatch.setattr(llm_mod, "Agent", factory)
    return factory


def test_recovers_markdown_fenced_json(monkeypatch) -> None:
    payload = {"summary": "ok", "files": {"a.py": "x"}}
    factory = _patch_agent(monkeypatch, "```json\n" + json.dumps(payload) + "\n```")
    result = complete_json_with_continuation(object(), "prompt")
    assert result == payload
    assert factory.instance.calls[0]["prompt"] == "prompt"


def test_recovers_prose_prefixed_json(monkeypatch) -> None:
    payload = {"summary": "ok"}
    _patch_agent(monkeypatch, "Here's the JSON:\n" + json.dumps(payload))
    result = complete_json_with_continuation(object(), "prompt")
    assert result == payload


def test_recovers_trailing_comma(monkeypatch) -> None:
    _patch_agent(monkeypatch, '{"summary": "ok", "files": {},}')
    result = complete_json_with_continuation(object(), "prompt")
    assert result == {"summary": "ok", "files": {}}


def test_raises_llm_json_parse_error_when_unrecoverable(monkeypatch) -> None:
    _patch_agent(monkeypatch, "no json anywhere in this response at all")
    with pytest.raises(LLMJsonParseError):
        complete_json_with_continuation(object(), "prompt")


def test_bare_json_loads_still_works_directly(monkeypatch) -> None:
    _patch_agent(monkeypatch, '{"summary": "clean"}')
    result = complete_json_with_continuation(object(), "prompt")
    assert result == {"summary": "clean"}


def test_client_resolution_does_not_discard_raw_llm_client(monkeypatch) -> None:
    """Regression test: a plain (non-Strands-Model) LLMClient must be forwarded
    to get_strands_model as the client= kwarg, not silently dropped in favor of
    a fresh default model."""

    class _FakeClient(LLMClient):
        def complete_json(self, *a, **kw):  # pragma: no cover - not exercised
            return {}

        def complete_text(self, *a, **kw):  # pragma: no cover - not exercised
            return ""

    captured = {}

    def fake_get_strands_model(agent_key=None, **kwargs):
        captured["agent_key"] = agent_key
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(llm_mod, "get_strands_model", fake_get_strands_model)
    _patch_agent(monkeypatch, '{"ok": true}')

    client = _FakeClient()
    complete_json_with_continuation(client, "prompt", task_id="my-agent")

    assert captured["kwargs"].get("client") is client


def test_strands_model_client_passed_through_unwrapped(monkeypatch) -> None:
    """When client is already a Strands Model, it's used as-is (no re-resolution)."""
    from strands.models.model import Model as StrandsModel

    class _M(StrandsModel):
        def update_config(self, *a, **kw):
            pass

        def get_config(self):
            return {}

        def structured_output(self, *a, **kw):  # pragma: no cover
            return {}

        async def stream(self, *a, **kw):  # pragma: no cover
            yield {}

    model = _M()
    factory = _patch_agent(monkeypatch, '{"ok": true}')
    complete_json_with_continuation(model, "prompt")
    assert factory.constructed_with["model"] is model


def test_system_prompt_override_reaches_agent(monkeypatch) -> None:
    factory = _patch_agent(monkeypatch, '{"ok": true}')
    complete_json_with_continuation(object(), "prompt", system_prompt="Custom instructions")
    assert factory.constructed_with["system_prompt"] == "Custom instructions"


def test_default_system_prompt_used_when_not_overridden(monkeypatch) -> None:
    factory = _patch_agent(monkeypatch, '{"ok": true}')
    complete_json_with_continuation(object(), "prompt")
    assert factory.constructed_with["system_prompt"] == llm_mod.DEFAULT_JSON_SYSTEM_PROMPT


def test_think_true_forwarded_to_invocation(monkeypatch) -> None:
    factory = _patch_agent(monkeypatch, '{"ok": true}')
    complete_json_with_continuation(object(), "prompt", think=True)
    assert factory.instance.calls[0]["kwargs"]["think"] is True


def test_think_none_not_forwarded_to_invocation(monkeypatch) -> None:
    """think=None (the default) must not appear in invocation kwargs, so it
    doesn't clobber a model-level think config baked in at construction time."""
    factory = _patch_agent(monkeypatch, '{"ok": true}')
    complete_json_with_continuation(object(), "prompt")
    assert "think" not in factory.instance.calls[0]["kwargs"]


def test_temperature_always_forwarded(monkeypatch) -> None:
    factory = _patch_agent(monkeypatch, '{"ok": true}')
    complete_json_with_continuation(object(), "prompt", temperature=0.7)
    assert factory.instance.calls[0]["kwargs"]["temperature"] == 0.7
