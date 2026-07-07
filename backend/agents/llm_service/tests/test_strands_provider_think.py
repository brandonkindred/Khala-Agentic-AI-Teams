"""Tests for ``get_strands_model(..., think=...)``.

The thinking-level override lets a caller (e.g. the code-review last-resort
thinking-off retry) force reasoning off for one adapter. It must forward to
``LLMClientModel`` and, because ``think`` is not part of the model cache key,
bypass the cache so a cached default-thinking model is never served for an
explicit override.
"""

from __future__ import annotations

import pytest

from llm_service.clients.dummy import DummyLLMClient
from llm_service.strands_provider import (
    _clear_strands_model_cache_for_testing,
    get_strands_model,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Empty the Strands model cache before and after each test."""
    _clear_strands_model_cache_for_testing()
    yield
    _clear_strands_model_cache_for_testing()


def test_think_forwards_via_client_path():
    """An explicit ``think`` is threaded into the wrapped ``LLMClientModel``."""
    dummy = DummyLLMClient()
    assert get_strands_model(client=dummy, think=False).get_config().get("think") is False
    # Default (no override) keeps the provider/model default.
    assert get_strands_model(client=dummy).get_config().get("think") is None


def test_think_bypasses_cache_default_path(monkeypatch):
    """The default (cached) path stays cached, but an explicit ``think`` builds a
    fresh, uncached adapter each call (so it can't collide on the think-less key)."""
    monkeypatch.setenv("LLM_PROVIDER", "dummy")

    a = get_strands_model("code_review", think=False)
    b = get_strands_model("code_review", think=False)
    assert a is not b  # explicit-think adapters are never cached
    assert a.get_config().get("think") is False and b.get_config().get("think") is False

    # The think-less default path is still cached (same instance reused).
    c = get_strands_model("code_review")
    d = get_strands_model("code_review")
    assert c is d
    assert c.get_config().get("think") is None
