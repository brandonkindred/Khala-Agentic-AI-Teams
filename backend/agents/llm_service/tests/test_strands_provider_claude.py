"""Provider-aware model_id in get_strands_model under LLM_PROVIDER=claude.

Lives in its own focused file alongside the other Strands provider suites.
"""

from __future__ import annotations

import pytest

from llm_service import _clear_strands_model_cache_for_testing, clear_client_cache
from llm_service import provider_store as ps
from llm_service.strands_provider import get_strands_model


@pytest.fixture(autouse=True)
def _claude_env(monkeypatch):
    """Seed a Claude provider entry (its own key) and clear caches around each test.

    The provider list is the sole source of LLM resolution, so get_client needs a
    configured entry. The Strands model_id is still derived from the shared
    resolvers (provider=claude), independent of the entry's own model.
    """
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    entry = ps.ProviderEntry(
        id=1,
        label="e",
        provider="claude",
        model="claude-opus-4-8",
        base_url="",
        api_key="sk-test",
        sort_order=1,
        limit_exceeded=False,
        limit_type="",
        reset_at=None,
    )
    monkeypatch.setattr(ps, "load_ordered_entries", lambda *a, **k: [entry])
    monkeypatch.setattr(ps, "select_active_entry", lambda es, **k: es[0])
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
