"""Provider-aware model_id in get_strands_model under LLM_PROVIDER=claude.

Lives in its own focused file alongside the other Strands provider suites.
"""

from __future__ import annotations

import pytest

from llm_service import _clear_strands_model_cache_for_testing, clear_client_cache
from llm_service.strands_provider import get_strands_model


@pytest.fixture(autouse=True)
def _claude_env(monkeypatch):
    """Select the Claude provider with a test key and clear caches around each test."""
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    _clear_strands_model_cache_for_testing()
    clear_client_cache()
    yield
    _clear_strands_model_cache_for_testing()
    clear_client_cache()


def test_strands_model_id_is_claude_under_claude_provider():
    """Under the Claude provider the Strands model_id is the Claude model, not the Ollama one."""
    model = get_strands_model("backend")
    # Must be the Claude model, not the Ollama-resolved one (telemetry/cache tag).
    assert model.get_config()["model_id"] == "claude-opus-4-8"


def test_strands_model_id_uses_claude_global_env(monkeypatch):
    """A Claude global LLM_MODEL is reflected in the Strands model_id."""
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    _clear_strands_model_cache_for_testing()
    model = get_strands_model(None)
    assert model.get_config()["model_id"] == "claude-sonnet-4-6"
