"""Tests for lazy LLM provider resolution.

Resolving a provider eagerly at import time crashes container startup when no
provider is configured. ``get_llm_client(..., lazy=True)`` must defer resolution
until the first actual LLM call so the service can start and serve the
``/llm-config`` setup flow.
"""

from unittest.mock import patch

import pytest

from llm_service import LLMNotConfiguredError

from ..models import AssistantRequest
from ..orchestrator.agent import PersonalAssistantOrchestrator
from ..shared.llm import LLMClient, _LazyPALLMClient, get_llm_client


def test_lazy_client_does_not_resolve_on_construction():
    """A lazy client must not touch the provider layer when constructed."""
    with patch(
        "personal_assistant_team.shared.llm.get_llm_client_with_pa_exceptions"
    ) as resolve:
        client = get_llm_client("personal_assistant", lazy=True)
        assert isinstance(client, _LazyPALLMClient)
        resolve.assert_not_called()
        assert client._delegate is None


def test_lazy_client_resolves_on_first_call_and_caches():
    """First invocation resolves the backing client exactly once, then reuses it."""

    class _Recording(LLMClient):
        def __init__(self):
            super().__init__()
            self._provider = "mock"
            self.calls = 0

        def _ollama_complete(self, prompt: str, **kwargs) -> str:
            self.calls += 1
            return "raw"

        def complete(self, prompt: str, **kwargs) -> str:
            self.calls += 1
            return "done"

        def complete_json(self, prompt: str, **kwargs):
            return {"ok": True}

        def get_max_context_tokens(self) -> int:
            return 4096

    backing = _Recording()
    with patch(
        "personal_assistant_team.shared.llm.get_llm_client_with_pa_exceptions",
        return_value=backing,
    ) as resolve:
        client = get_llm_client("personal_assistant", lazy=True)
        assert client.complete("hi") == "done"
        assert client.complete_json("hi") == {"ok": True}
        assert client._ollama_complete("hi") == "raw"
        assert client.get_max_context_tokens() == 4096
        # Resolved once, cached thereafter.
        resolve.assert_called_once_with("personal_assistant")


def test_eager_default_resolves_immediately():
    """Without ``lazy=True`` the provider is resolved eagerly (existing behavior)."""
    sentinel = object()
    with patch(
        "personal_assistant_team.shared.llm.get_llm_client_with_pa_exceptions",
        return_value=sentinel,
    ) as resolve:
        result = get_llm_client("personal_assistant")
        assert result is sentinel
        resolve.assert_called_once_with("personal_assistant")


class _UnconfiguredLLM(LLMClient):
    """Stands in for the lazy client when no provider is configured: every call
    raises ``LLMNotConfiguredError`` (as ``get_client`` does on an empty list)."""

    def __init__(self):
        super().__init__()
        self._provider = "mock"

    def _ollama_complete(self, prompt: str, **kwargs) -> str:
        raise LLMNotConfiguredError("No LLM provider is configured.")

    def complete(self, prompt: str, **kwargs) -> str:
        raise LLMNotConfiguredError("No LLM provider is configured.")

    def complete_json(self, prompt: str, **kwargs):
        raise LLMNotConfiguredError("No LLM provider is configured.")

    def get_max_context_tokens(self) -> int:
        return 4096


def test_handle_request_propagates_missing_provider():
    """A missing provider must fail the run (propagate), not be swallowed into a
    completed job with a degraded response."""
    orch = PersonalAssistantOrchestrator(_UnconfiguredLLM())
    request = AssistantRequest(
        request_id="req-1", user_id="u1", message="what's on my calendar today?"
    )
    with pytest.raises(LLMNotConfiguredError):
        orch.handle_request(request)


def test_classify_intent_does_not_swallow_missing_provider():
    """The intent-classification catch-all must re-raise ``LLMNotConfiguredError``."""
    orch = PersonalAssistantOrchestrator(_UnconfiguredLLM())
    with pytest.raises(LLMNotConfiguredError):
        orch.classify_intent("hello")
