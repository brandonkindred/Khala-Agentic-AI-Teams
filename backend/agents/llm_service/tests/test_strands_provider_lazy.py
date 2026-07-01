"""Tests for ``get_strands_model(..., lazy=True)``.

Resolving a provider eagerly at import time (e.g. a team's process-wide
orchestrator singleton calling ``get_strands_model`` in ``__init__``) fails
hard with ``LLMNotConfiguredError`` when no provider is configured, which can
crash container startup before the service can serve health checks or the
``/llm-config`` setup flow. ``lazy=True`` must defer that resolution until the
first actual model call.
"""

from __future__ import annotations

import pytest

import llm_service.strands_provider as sp
from llm_service.interface import LLMNotConfiguredError
from llm_service.strands_provider import (
    _clear_strands_model_cache_for_testing,
    _LazyLLMClientModel,
    get_strands_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Empty the Strands model cache before and after each test."""
    _clear_strands_model_cache_for_testing()
    yield
    _clear_strands_model_cache_for_testing()


def test_lazy_model_does_not_resolve_on_construction(monkeypatch):
    """Constructing a lazy model must not touch the provider layer at all."""
    resolve_calls = []
    monkeypatch.setattr(sp, "get_client", lambda *a, **kw: resolve_calls.append((a, kw)))

    model = get_strands_model("nutrition_meal_planning", lazy=True)

    assert isinstance(model, _LazyLLMClientModel)
    assert resolve_calls == []
    assert model._delegate is None


def test_lazy_model_rejects_explicit_client():
    """``lazy=True`` with an explicit ``client`` is a caller contract violation."""
    with pytest.raises(AssertionError):
        get_strands_model("nutrition_meal_planning", lazy=True, client=object())


def test_lazy_model_resolves_on_first_call_and_caches(monkeypatch):
    """First real call resolves the backing model exactly once, then reuses it."""
    calls = {"n": 0}

    def _fake_get_client(agent_key=None, **kwargs):
        calls["n"] += 1
        from llm_service.clients.dummy import DummyLLMClient

        return DummyLLMClient()

    monkeypatch.setattr(sp, "get_client", _fake_get_client)

    model = get_strands_model("nutrition_meal_planning", lazy=True)
    assert model.get_max_context_tokens() > 0
    assert model.get_max_context_tokens() > 0

    assert calls["n"] == 1


def test_lazy_model_propagates_missing_provider_error(monkeypatch):
    """A genuinely unconfigured provider must fail the caller, not silently degrade."""

    def _raise(*args, **kwargs):
        raise LLMNotConfiguredError("No LLM provider is configured.")

    monkeypatch.setattr(sp, "get_client", _raise)

    model = get_strands_model("nutrition_meal_planning", lazy=True)
    with pytest.raises(LLMNotConfiguredError):
        model.get_max_context_tokens()
