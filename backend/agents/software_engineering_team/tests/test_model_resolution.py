"""Tests for the code-review agents' shared strands-model resolution.

``resolve_code_review_model`` and ``resolve_code_review_verify_model`` share
the same shape: return an injected strands ``Model`` unchanged (the test path),
else build the production model via ``get_strands_model`` keyed on the agent's
own agent key. These tests pin that both the injection short-circuit and the
production ``get_strands_model`` call site are correct and independent for the
new ``code_review_verify`` key.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from code_review_agent import model_resolution
from strands.models.model import Model as _StrandsModel


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

    dummy_llm = MagicMock()
    result = model_resolution.resolve_code_review_verify_model(dummy_llm)
    assert result is sentinel
    assert calls == [(("code_review_verify",), {})]

    calls.clear()
    result = model_resolution.resolve_code_review_verify_model(dummy_llm, think="low")
    assert result is sentinel
    assert calls == [(("code_review_verify",), {"think": "low"})]


def test_thinking_override_supported_unaffected_by_new_key() -> None:
    fake = _FakeStrandsModel()
    assert model_resolution.thinking_override_supported(fake) is False
    assert model_resolution.thinking_override_supported(MagicMock()) is True
