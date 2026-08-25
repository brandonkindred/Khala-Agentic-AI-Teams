"""Tests for AccessibilityExpertAgent's shared review-result cache.

Mirrors ``test_qa_agent_cache.py``'s conventions for the analogous QA and
security review caches: a ``_CountingClient`` counts LLM invocations so a
hit (no call) can be distinguished from a miss (a call). See
``accessibility_agent/agent.py``'s module-level comment for how this cache's
key *shape* (whole-input hash) and caching *policy* (every genuine outcome,
not just approved ones) mirror ``qa_agent``'s and ``security_agent``'s.

The cache itself (``shared.cache`` — Redis when configured, otherwise an
in-process store) is cleared around every test by the autouse
``_reset_accessibility_review_cache`` fixture in ``conftest.py``, so tests do
not observe cross-test cache hits.
"""

from __future__ import annotations

from typing import Any, Dict

import accessibility_agent.agent as agent_mod
import pytest
from accessibility_agent import AccessibilityExpertAgent, AccessibilityInput
from accessibility_agent.models import AccessibilityOutput

import software_engineering_team.shared.review_result_cache as review_cache_mod
from llm_service.clients.dummy import DummyLLMClient
from llm_service.strands_model import model_fingerprint
from shared.cache import get_shared_cache
from shared.cache.pydantic_cache import build_model_cache_key

_CLEAN_RESPONSE: Dict[str, Any] = {
    "issues": [],
    "approved": True,
    "summary": "looks fine, no wcag issues",
}


class _CountingClient(DummyLLMClient):
    """Returns a fixed canned response; counts ``complete_json`` calls.

    ``DummyLLMClient.stream()`` (the method Strands' event loop calls for a
    structured-output request) routes through ``complete_json`` — see
    ``llm_service/clients/dummy.py`` — so overriding it here counts exactly
    the LLM invocations a cache hit is meant to skip.
    """

    def __init__(self, response: Dict[str, Any]) -> None:
        super().__init__()
        self._response = response
        self.calls = 0

    def complete_json(
        self, prompt: str, *, temperature=0.0, system_prompt=None, tools=None, think=False, **kwargs
    ) -> Dict[str, Any]:  # type: ignore[override]
        self.calls += 1
        return dict(self._response)


def _input(**overrides: object) -> AccessibilityInput:
    base: Dict[str, object] = {
        "code": '<button onclick="x()">Click</button>',
        "language": "html",
        "task_description": "Accessibility review of click handler",
    }
    base.update(overrides)
    return AccessibilityInput(**base)  # type: ignore[arg-type]


def test_identical_review_hits_cache_and_skips_llm_call() -> None:
    """A byte-identical ``AccessibilityInput`` resubmission hits the cache
    and skips the LLM call."""
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = AccessibilityExpertAgent(client)

    first = agent.run(_input())
    second = agent.run(_input())

    assert client.calls == 1
    assert first.model_dump() == second.model_dump()


def test_changed_code_busts_cache() -> None:
    """A reviewed-file byte change naturally busts the key -- no explicit
    invalidation logic needed."""
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = AccessibilityExpertAgent(client)

    agent.run(_input())
    agent.run(_input(code='<button aria-label="x">Click</button>'))

    assert client.calls == 2


def test_cache_backend_error_falls_open_to_correct_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any cache backend error (get/set) must never abort a review."""

    class _RaisingCache:
        def get(self, key: str) -> None:
            raise RuntimeError("boom")

        def set(self, key: str, value: bytes, *, max_entries: int) -> None:
            raise RuntimeError("boom")

        def delete(self, key: str) -> None:
            raise RuntimeError("boom")

        def clear(self) -> None:
            # Not exercised by run() itself; only needed so the autouse
            # conftest teardown (which calls clear_review_cache()) doesn't
            # blow up while the cache module's get_shared_cache is still
            # monkeypatched.
            pass

    monkeypatch.setattr(review_cache_mod, "get_shared_cache", lambda namespace: _RaisingCache())

    client = _CountingClient(_CLEAN_RESPONSE)
    agent = AccessibilityExpertAgent(client)
    result = agent.run(_input())

    assert isinstance(result, AccessibilityOutput)
    assert result.approved is True
    assert client.calls == 1


def test_unapproved_result_is_still_cached() -> None:
    """Every genuine outcome is cached regardless of ``approved`` --
    AccessibilityExpertAgent.run() is a single atomic call with no reduce
    phase to re-run on a retry.

    Asserts on ``issues`` (the actual precondition: a non-clean result)
    rather than the derived ``approved`` flag -- that derivation is
    ``test_accessibility_agent.py``'s concern, not this cache test's.
    """
    response = dict(_CLEAN_RESPONSE)
    response["issues"] = [
        {
            "severity": "critical",
            "wcag_criterion": "1.1.1",
            "description": "Missing alt text on img",
        }
    ]
    client = _CountingClient(response)
    agent = AccessibilityExpertAgent(client)

    first = agent.run(_input())
    second = agent.run(_input())

    assert first.issues  # not a clean result
    assert client.calls == 1
    assert second.model_dump() == first.model_dump()


def test_fallback_result_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured-output failure must be retried for real next time, not
    frozen into the cache as a permanent 'no issues' verdict."""

    class _RaisingAgent:
        def __call__(self, *a: object, **kw: object) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_mod, "Agent", lambda *, model, system_prompt: _RaisingAgent())

    agent = AccessibilityExpertAgent(DummyLLMClient())
    input_data = _input()
    result = agent.run(input_data)
    assert result.approved is False

    key = build_model_cache_key(input_data, model_fingerprint(agent._model))
    cache = get_shared_cache(agent_mod._REVIEW_CACHE._namespace())
    assert cache.get(key) is None


def test_cache_disabled_via_env_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting ``ACCESSIBILITY_REVIEW_CACHE_SIZE=0`` disables the cache;
    every call invokes the model."""
    monkeypatch.setenv("ACCESSIBILITY_REVIEW_CACHE_SIZE", "0")
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = AccessibilityExpertAgent(client)

    agent.run(_input())
    agent.run(_input())

    assert client.calls == 2


def test_cache_disabled_via_env_ignores_stale_pre_existing_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-existing cache entry (e.g. written before a restart with
    ``ACCESSIBILITY_REVIEW_CACHE_SIZE=0``) must never be served once the
    cache is disabled -- disabling the cache means every call re-invokes the
    model, not just that new results stop being written."""
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = AccessibilityExpertAgent(client)
    input_data = _input()

    stale = dict(_CLEAN_RESPONSE)
    stale["summary"] = "stale cached verdict"
    key = build_model_cache_key(input_data, model_fingerprint(agent._model))
    cache = get_shared_cache(agent_mod._REVIEW_CACHE._namespace())
    cache.set(key, AccessibilityOutput(**stale).model_dump_json().encode(), max_entries=256)

    monkeypatch.setenv("ACCESSIBILITY_REVIEW_CACHE_SIZE", "0")
    result = agent.run(input_data)

    assert client.calls == 1
    assert result.summary != "stale cached verdict"
