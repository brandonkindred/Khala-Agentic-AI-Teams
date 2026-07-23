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
    )

    assert result == {"ready": True, "rationale": "ok", "issues": []}
    assert len(client.calls) == 1
    assert client.calls[0]["schema"] == {"type": "object"}
    assert client.calls[0]["objective"] == "strategy design review (structured)"
    assert client.calls[0]["system_prompt"] == "sys"


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
        )


def test_invoke_structured_with_schema_rejects_empty_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(so_mod, "structured_output_available", lambda: True)
    with pytest.raises(AssertionError, match="precondition"):
        so_mod.invoke_structured_with_schema(
            "",
            "sys",
            "user",
            phase="design_generate_structured",
            schema={"type": "object"},
            charge=True,
            objective="strategy design (structured)",
            logger=logging.getLogger("test.so"),
        )


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
        )
