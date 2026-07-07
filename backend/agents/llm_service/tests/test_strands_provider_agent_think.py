"""Tests for per-agent default thinking level plumbed through ``get_strands_model``.

Some agents run a reasoning model in JSON mode where the model's top ``max``
reasoning tier produces content-free, reasoning-only turns (semantic exhaustion).
``get_strands_model`` pins a reduced default tier for those agents (via
``config.AGENT_DEFAULT_THINK``) so the FIRST call already runs at a tier that
opens the content channel, rather than relying on the client's post-hoc downgrade.
"""

from __future__ import annotations

import pytest

import llm_service.strands_provider as sp
from llm_service.strands_provider import (
    _clear_strands_model_cache_for_testing,
    get_strands_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    _clear_strands_model_cache_for_testing()
    yield
    _clear_strands_model_cache_for_testing()


@pytest.fixture(autouse=True)
def _dummy_provider(monkeypatch):
    """Route resolution through the dummy provider so no live LLM/Postgres is needed."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    def _fake_get_client(agent_key=None, **kwargs):
        from llm_service.clients.dummy import DummyLLMClient

        return DummyLLMClient()

    monkeypatch.setattr(sp, "get_client", _fake_get_client)


def test_code_review_model_defaults_to_reduced_thinking() -> None:
    """code_review's strands model is configured with the reduced ``high`` tier."""
    model = get_strands_model("code_review")
    assert model.get_config()["think"] == "high"


def test_unlisted_agent_leaves_thinking_unset() -> None:
    """An agent with no pinned tier keeps ``think=None`` (model platform default)."""
    model = get_strands_model("backend")
    assert model.get_config()["think"] is None


def test_client_supplied_path_also_pins_agent_thinking() -> None:
    """The explicit-``client`` branch (cache-bypassing) applies the pinned tier too."""
    from llm_service.clients.dummy import DummyLLMClient

    model = get_strands_model("code_review", client=DummyLLMClient())
    assert model.get_config()["think"] == "high"
