"""Unit tests for shared Strategy Lab structured-output invoke helper."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import pytest

from investment_team.strategy_lab.agents import _structured_output as so_mod


class _StubClient:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []
        self.reasoning_calls: List[Dict[str, Any]] = []

    def complete(self, prompt: str, **kwargs: Any) -> str:
        # invoke_structured_with_schema's think=True reasoning pass, run
        # before the schema-conformant complete_json call below.
        self.reasoning_calls.append({"prompt": prompt, **kwargs})
        return "reasoning prose"

    def complete_json(self, prompt: str, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append({"prompt": prompt, **kwargs})
        return dict(self.payload)


class _FakeModel:
    def __init__(self, client: _StubClient) -> None:
        self.client = client


def test_invoke_structured_with_schema_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _StubClient({"ready": True, "rationale": "ok", "issues": []})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))

    result = so_mod.invoke_structured_with_schema(
        "strategy_design_review",
        "sys",
        "user",
        phase="design_review_structured",
        schema={"type": "object"},
        charge=False,
        objective="strategy design review (structured)",
        logger=logging.getLogger("test.so"),
        reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
    )

    assert result == {"ready": True, "rationale": "ok", "issues": []}
    assert len(client.reasoning_calls) == 1
    assert client.reasoning_calls[0]["think"] is True
    assert client.reasoning_calls[0]["objective"] == "strategy design review (structured) (reasoning)"
    assert client.reasoning_calls[0]["system_prompt"] == "sys" + so_mod.REASONING_MODE_SUFFIX

    assert len(client.calls) == 1
    assert client.calls[0]["schema"] == {"type": "object"}
    assert client.calls[0]["objective"] == "strategy design review (structured) (format)"
    assert client.calls[0]["system_prompt"] == "sys"
    assert client.calls[0]["think"] is False
    # The formatting prompt carries both the original user prompt and the
    # reasoning-pass prose.
    assert "user" in client.calls[0]["prompt"]
    assert "reasoning prose" in client.calls[0]["prompt"]


def test_invoke_structured_with_schema_doubles_timeout_for_two_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_call`` now makes two sequential provider calls (reasoning + format)
    under the envelope's single per-attempt timeout guard. Regression test
    for a real Codex review finding: without doubling, two individually
    healthy calls could together exceed a budget sized for one, aborting the
    attempt and abandoning a still-running daemon thread even though neither
    provider request was actually slow.
    """
    client = _StubClient({"ready": True, "rationale": "ok", "issues": []})
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    monkeypatch.setattr(so_mod, "get_strands_model", lambda *_a, **_k: _FakeModel(client))
    monkeypatch.setattr(so_mod, "resolve_timeout", lambda agent_key: 30.0)
    monkeypatch.delenv("STRATEGY_LAB_LLM_TIMEOUT", raising=False)

    captured: Dict[str, Any] = {}

    def _spy_run_structured_agent(agent_callable, prompt, *, parse, **kwargs):
        captured.update(kwargs)
        return parse(agent_callable(prompt))

    monkeypatch.setattr(so_mod, "run_structured_agent", _spy_run_structured_agent)

    result = so_mod.invoke_structured_with_schema(
        "strategy_design_review",
        "sys",
        "user",
        phase="design_review_structured",
        schema={"type": "object"},
        charge=False,
        objective="strategy design review (structured)",
        logger=logging.getLogger("test.so"),
        reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
    )

    assert result == {"ready": True, "rationale": "ok", "issues": []}
    assert captured["timeout_s"] == pytest.approx(60.0)


def test_invoke_structured_with_schema_requires_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: False)
    with pytest.raises(AssertionError, match="precondition"):
        so_mod.invoke_structured_with_schema(
            "strategy_design",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={"type": "object"},
            charge=True,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        )


@pytest.mark.parametrize("field", ["agent_key", "system_prompt", "user_prompt"])
def test_invoke_structured_with_schema_rejects_empty_inputs(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    kwargs: Dict[str, Any] = {
        "agent_key": "strategy_design",
        "system_prompt": "sys",
        "user_prompt": "user",
        "phase": "design_generate_structured",
        "schema": {"type": "object"},
        "charge": True,
        "objective": "strategy design (structured)",
        "logger": logging.getLogger("test.so"),
        "reasoning_system_prompt": "sys" + so_mod.REASONING_MODE_SUFFIX,
    }
    kwargs[field] = ""
    with pytest.raises(AssertionError, match="precondition"):
        so_mod.invoke_structured_with_schema(**kwargs)


def test_invoke_structured_with_schema_rejects_empty_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    with pytest.raises(AssertionError, match="precondition"):
        so_mod.invoke_structured_with_schema(
            "strategy_design",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={},
            charge=True,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
            reasoning_system_prompt="sys" + so_mod.REASONING_MODE_SUFFIX,
        )
