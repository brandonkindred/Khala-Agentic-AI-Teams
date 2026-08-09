"""Tests for ``llm_service.strands_model.resolve_strands_model``.

This helper collapses 22 duplicated copies of the same isinstance-check pattern
across the v2 phase ``_resolve_model`` helpers and the v2 tool-agent
``__init__`` blocks. The tests pin the three branches plus the fix for the
pre-existing tool-agent gap (raw LLMClient injections silently discarded).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from llm_service import DummyLLMClient, OllamaLLMClient
from llm_service.strands_adapter import LLMClientModel
from llm_service.strands_model import resolve_strands_model


@pytest.fixture(autouse=True)
def _isolated_factory_cache(monkeypatch):
    """Use the dummy provider and clear caches so each test sees a fresh state."""
    from llm_service import factory
    from llm_service.strands_provider import _clear_strands_model_cache_for_testing

    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    factory.clear_client_cache()
    _clear_strands_model_cache_for_testing()
    yield
    _clear_strands_model_cache_for_testing()


def test_resolve_returns_strands_model_as_is_when_passed_a_model() -> None:
    """If the caller already has a Strands ``Model``, return it untouched —
    they're pinning a specific mode and we must not wrap or re-create it."""
    backing = DummyLLMClient()
    pre_built = LLMClientModel(backing, response_format="json")

    result = resolve_strands_model(pre_built, response_format="text")

    # Identity preserved — no wrapping, no clone, no mode override.
    assert result is pre_built
    assert result.get_config()["response_format"] == "json"


def test_resolve_wraps_llm_client_with_requested_response_format() -> None:
    """The v2 phase / tool-agent path: the orchestrator hands in a raw
    ``LLMClient`` (e.g. ``OllamaLLMClient``). The helper must wrap it as a
    Strands ``Model`` *while honoring the requested response_format* — the
    pre-helper tool-agent code silently discarded the injection by falling to
    a default ``get_strands_model()`` call.
    """
    client = OllamaLLMClient(model="some-model", base_url="http://localhost:11434", timeout=5)

    result = resolve_strands_model(client, response_format="text")

    assert isinstance(result, LLMClientModel)
    # Same backing client — retries/telemetry/rate-limit guard preserved.
    assert result._client is client
    assert result.get_config()["response_format"] == "text"


def test_resolve_falls_back_to_default_when_llm_is_none() -> None:
    """``llm=None`` (or any non-Model, non-LLMClient value) constructs a
    default Strands model via the provider with the requested
    response_format. This is the path the v2 phase helpers hit when no LLM
    is injected at all."""
    result = resolve_strands_model(None, response_format="text")

    assert isinstance(result, LLMClientModel)
    assert result.get_config()["response_format"] == "text"


def test_resolve_falls_back_when_llm_is_unrecognized_object() -> None:
    """A defensive case: a caller hands us something that's neither a Strands
    Model nor an LLMClient. The helper must not raise — fall through to the
    default path so the test fixture's MagicMock-style stubs keep working."""
    bogus = MagicMock(name="not-a-model-or-client")

    result = resolve_strands_model(bogus, response_format="json")

    assert isinstance(result, LLMClientModel)
    assert result.get_config()["response_format"] == "json"


def test_resolve_default_response_format_is_json() -> None:
    """Match ``llm_service.get_strands_model``'s default. Callers asking for
    JSON output (the safer default for unaudited callers) don't need to pass
    the keyword explicitly."""
    result = resolve_strands_model(None)

    assert result.get_config()["response_format"] == "json"
