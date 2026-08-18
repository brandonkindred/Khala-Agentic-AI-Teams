"""Tests for the code-review agents' shared strands-model resolution.

``resolve_code_review_model`` and ``resolve_code_review_verify_model`` share
the same shape:

- when given an injected strands ``Model``, return it unchanged (the test
  path short-circuit)
- otherwise resolve the production model via ``get_strands_model`` under
  the correct agent key.

The verify resolver additionally pins Ollama failover candidates so a filled
provider-list ``entry.model`` cannot shadow the lighter verify selection.
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


def test_resolve_code_review_verify_model_non_model_triggers_production_path(
    monkeypatch,
) -> None:
    """Non-``Model`` input must not short-circuit the injected path."""
    sentinel = MagicMock()
    calls: list[tuple[Any, ...]] = []

    def _fake_get_strands_model(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return sentinel

    # For a non-Model sentinel, the helper falls back to `with_model_override`,
    # which we stub to be a no-op.
    monkeypatch.setattr(model_resolution, "get_strands_model", _fake_get_strands_model)
    monkeypatch.setattr(model_resolution, "with_model_override", lambda client, _m: client)

    plain_client = MagicMock()
    result = model_resolution.resolve_code_review_verify_model(plain_client)
    assert result is sentinel
    assert calls == [(("code_review_verify",), {})]


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
    pin = AGENT_DEFAULT_MODELS["code_review_verify"]
    pinned_backing.model = pin  # Ollama path: active .model matches the pin
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
    assert pin_calls == [(backing, pin)]
    assert isinstance(result, LLMClientModel)
    assert result.client is pinned_backing
    assert result.get_config()["model_id"] == pin
    assert result.get_config()["agent_key"] == "code_review_verify"


def test_resolve_code_review_verify_model_preserves_model_id_for_claude_active(
    monkeypatch,
) -> None:
    """When the active failover candidate is Claude, keep the adapter model_id.

    ``with_model_override`` leaves Claude candidates on their configured model;
    relabeling ``model_id`` to the Ollama pin would mis-report observability.

    The adapter's original ``model_id`` and the pinned backing's active
    ``.model`` are deliberately distinct values here: if they were equal (as
    in an earlier version of this test), the assertion could not tell
    "preserved the original" apart from "relabeled to whatever the active
    backing reports" -- both would produce the same expected string. Distinct
    values make the assertion actually exercise the preservation behavior the
    test name and docstring claim.
    """
    backing = MagicMock(name="failover_client")
    base = LLMClientModel(backing, agent_key="code_review_verify", model_id="claude-sonnet-4-5")
    pinned_backing = MagicMock(name="pinned_failover")
    # Distinct from base's model_id -- see docstring above.
    pinned_backing.model = "claude-opus-4-8"  # Claude path: pin ignored

    monkeypatch.setattr(model_resolution, "get_strands_model", lambda *_a, **_k: base)
    monkeypatch.setattr(
        model_resolution, "with_model_override", lambda client, _model: pinned_backing
    )
    monkeypatch.delenv("LLM_MODEL_code_review_verify", raising=False)

    result = model_resolution.resolve_code_review_verify_model(MagicMock())
    assert isinstance(result, LLMClientModel)
    assert result.client is pinned_backing
    # The ORIGINAL adapter model_id survives, not the active backing's model
    # (which would be "claude-opus-4-8" if the code incorrectly relabeled).
    assert result.get_config()["model_id"] == "claude-sonnet-4-5"


def test_code_review_verify_model_pin_prefers_per_agent_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_code_review_verify", "custom-verify:7b")
    assert model_resolution._code_review_verify_model_pin() == "custom-verify:7b"
    monkeypatch.delenv("LLM_MODEL_code_review_verify", raising=False)
    assert (
        model_resolution._code_review_verify_model_pin()
        == AGENT_DEFAULT_MODELS["code_review_verify"]
    )


def test_resolve_code_review_verify_client_returns_given_llm_unchanged() -> None:
    sentinel = MagicMock()
    assert model_resolution.resolve_code_review_verify_client(sentinel) is sentinel


def test_resolve_code_review_verify_client_resolves_and_pins_when_none(monkeypatch) -> None:
    """``llm=None`` resolves ``get_client("code_review_verify")`` and applies the
    same Ollama failover pin as ``resolve_code_review_verify_model``, returning
    a raw ``LLMClient`` (not a strands ``Model``) for ``complete_json`` callers
    like ``run_single_shot_review``."""
    raw_client = MagicMock(name="raw_failover_client")
    pinned_client = MagicMock(name="pinned_client")
    calls: list[tuple[Any, ...]] = []

    def _fake_get_client(key: str) -> Any:
        calls.append(("get_client", key))
        return raw_client

    def _fake_with_model_override(client: Any, model: str) -> Any:
        calls.append(("with_model_override", client, model))
        assert client is raw_client
        return pinned_client

    monkeypatch.setattr(model_resolution, "get_client", _fake_get_client)
    monkeypatch.setattr(model_resolution, "with_model_override", _fake_with_model_override)
    monkeypatch.delenv("LLM_MODEL_code_review_verify", raising=False)

    result = model_resolution.resolve_code_review_verify_client()
    assert result is pinned_client
    assert calls[0] == ("get_client", "code_review_verify")
    assert calls[1][0] == "with_model_override"
    assert calls[1][2] == AGENT_DEFAULT_MODELS["code_review_verify"]


def test_thinking_override_supported_unaffected_by_new_key() -> None:
    fake = _FakeStrandsModel()
    assert model_resolution.thinking_override_supported(fake) is False
    assert model_resolution.thinking_override_supported(MagicMock()) is True


def test_resolve_code_review_model_forwards_response_format_text(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get(key: str, **kwargs: Any) -> object:
        seen.update(kwargs)
        seen["key"] = key
        return object()

    monkeypatch.setattr(model_resolution, "get_strands_model", fake_get)
    model_resolution.resolve_code_review_model(object(), response_format="text", think=True)
    assert seen["key"] == "code_review"
    assert seen["response_format"] == "text"
    assert seen["think"] is True


def test_resolve_code_review_verify_model_forwards_response_format_text(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_get(key: str, **kwargs: Any) -> MagicMock:
        seen.update(kwargs)
        seen["key"] = key
        return MagicMock()

    monkeypatch.setattr(model_resolution, "get_strands_model", fake_get)
    monkeypatch.setattr(model_resolution, "with_model_override", lambda client, _m: client)
    model_resolution.resolve_code_review_verify_model(object(), response_format="text", think=True)
    assert seen["key"] == "code_review_verify"
    assert seen["response_format"] == "text"
    assert seen["think"] is True


def test_resolve_code_review_model_clones_injected_model_for_text_mode() -> None:
    class _ClonableModel(_FakeStrandsModel):
        def get_config(self) -> dict[str, Any]:
            return {"response_format": "json"}

        def clone(self, **overrides: Any) -> "_ClonableModel":
            cloned = _ClonableModel()
            cloned._overrides = overrides  # type: ignore[attr-defined]
            return cloned

    base = _ClonableModel()
    result = model_resolution.resolve_code_review_model(base, response_format="text", think="low")
    assert result is not base
    assert result._overrides == {"response_format": "text", "think": "low"}  # type: ignore[attr-defined]


def test_resolve_code_review_model_uses_primary_agent_key(monkeypatch) -> None:
    """The primary resolver stays on the ``code_review`` agent key."""
    sentinel = MagicMock()
    calls: list[tuple[Any, ...]] = []

    def _fake_get_strands_model(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setattr(model_resolution, "get_strands_model", _fake_get_strands_model)
    monkeypatch.setattr(model_resolution, "with_model_override", lambda client, _m: client)

    dummy_llm = MagicMock()
    result = model_resolution.resolve_code_review_model(dummy_llm)
    assert result is sentinel
    assert calls == [(("code_review",), {})]
