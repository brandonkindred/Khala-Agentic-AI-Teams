"""Tests for the code-review agents' shared strands-model resolution.

``resolve_code_review_model`` and ``resolve_code_review_verify_model`` share
the same shape: return an injected strands ``Model`` unchanged (the test path),
else build the production model via ``get_strands_model`` keyed on the agent's
own agent key. The verify resolver additionally pins Ollama failover candidates
to the verify key's per-agent / default model so a filled provider-list
``entry.model`` cannot shadow the lighter selection.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from code_review_agent import model_resolution
from strands.models.model import Model as _StrandsModel

from llm_service.config import AGENT_DEFAULT_MODELS
from llm_service.strands_adapter import LLMClientModel


class _FakeStrandsModel(_StrandsModel):
    """Minimal strands ``Model`` stand-in for the injection path."""

    def update_config(self, **model_config: Any) -> None:  # pragma: no cover - unused
        pass

    def get_config(self) -> Any:  # pragma: no cover - unused
        return {}

    async def stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - unused
        yield {}

    async def structured_output(
        self, *args: Any, **kwargs: Any
    ) -> Any:  # pragma: no cover - unused
        yield {}


def test_resolve_code_review_verify_model_returns_injected_model_unchanged() -> None:
    fake = _FakeStrandsModel()
    assert model_resolution.resolve_code_review_verify_model(fake) is fake
    assert model_resolution.resolve_code_review_verify_model(fake, think="low") is fake


def test_resolve_code_review_verify_model_uses_its_own_agent_key(monkeypatch) -> None:
    """Production path resolves via get_strands_model("code_review_verify", ...),
    not the "code_review" key used by resolve_code_review_model."""
    sentinel = MagicMock()
    calls: list[tuple[Any, ...]] = []

    def _fake_get_strands_model(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(model_resolution, "get_strands_model", _fake_get_strands_model)
    # Non-failover / non-LLMClientModel → with_model_override is a no-op.
    monkeypatch.setattr(model_resolution, "with_model_override", lambda client, _model: client)

    dummy_llm = MagicMock()
    result = model_resolution.resolve_code_review_verify_model(dummy_llm)
    assert result is sentinel
    assert calls == [(("code_review_verify",), {})]

    calls.clear()
    result = model_resolution.resolve_code_review_verify_model(dummy_llm, think="low")
    assert result is sentinel
    assert calls == [(("code_review_verify",), {"think": "low"})]


def test_resolve_code_review_verify_model_pins_ollama_failover_candidates(monkeypatch) -> None:
    """Configured provider-list entry.model must not shadow the lighter verify model.

    ``get_strands_model`` alone only changes attribution when the active provider
    entry has a non-empty model; the verify resolver therefore pins Ollama
    failover candidates via ``with_model_override`` to the verify key's pin
    (``LLM_MODEL_code_review_verify`` or ``AGENT_DEFAULT_MODELS``).
    """
    backing = MagicMock(name="failover_client")
    base = LLMClientModel(backing, agent_key="code_review_verify", model_id="heavy:cloud")
    pinned_backing = MagicMock(name="pinned_failover")
    pin_calls: list[tuple[Any, str]] = []

    def _fake_get_strands_model(*_args: Any, **_kwargs: Any) -> Any:
        return base

    def _fake_with_model_override(client: Any, model: str) -> Any:
        pin_calls.append((client, model))
        assert client is backing
        return pinned_backing

    monkeypatch.setattr(model_resolution, "get_strands_model", _fake_get_strands_model)
    monkeypatch.setattr(model_resolution, "with_model_override", _fake_with_model_override)
    monkeypatch.delenv("LLM_MODEL_code_review_verify", raising=False)

    result = model_resolution.resolve_code_review_verify_model(MagicMock())
    assert pin_calls == [(backing, AGENT_DEFAULT_MODELS["code_review_verify"])]
    assert isinstance(result, LLMClientModel)
    assert result.client is pinned_backing
    assert result.get_config()["model_id"] == AGENT_DEFAULT_MODELS["code_review_verify"]
    assert result.get_config()["agent_key"] == "code_review_verify"


def test_code_review_verify_model_pin_prefers_per_agent_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_code_review_verify", "custom-verify:7b")
    assert model_resolution._code_review_verify_model_pin() == "custom-verify:7b"
    monkeypatch.delenv("LLM_MODEL_code_review_verify", raising=False)
    assert (
        model_resolution._code_review_verify_model_pin()
        == AGENT_DEFAULT_MODELS["code_review_verify"]
    )


def test_thinking_override_supported_unaffected_by_new_key() -> None:
    fake = _FakeStrandsModel()
    assert model_resolution.thinking_override_supported(fake) is False
    assert model_resolution.thinking_override_supported(MagicMock()) is True
