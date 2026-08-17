"""Tests for QAExpertAgent's shared review-result cache (issue #6113).

Mirrors ``test_code_review_cache.py``'s conventions for the analogous
code-review submission-level cache: a ``_CountingClient`` counts LLM
invocations so a hit (no call) can be distinguished from a miss (a call).
Unlike the code-review chunk map phase, ``QAExpertAgent.run()`` calls are
never parallelized against each other, so no locking is needed.

The cache itself (``shared.cache`` — Redis when configured, otherwise an
in-process store) is cleared around every test by the autouse
``_reset_qa_review_cache`` fixture in ``conftest.py``, so tests do not
observe cross-test cache hits.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
import qa_agent.agent as agent_mod
from qa_agent import QAExpertAgent, QAInput
from qa_agent.models import QAOutput

from llm_service.clients.dummy import DummyLLMClient
from shared.cache import MemoryBackend, get_shared_cache, reset_shared_cache_state
from shared.cache import factory as factory_mod

_CLEAN_RESPONSE: Dict[str, Any] = {
    "bugs_found": [],
    "approved": True,
    "summary": "looks fine",
    "integration_tests": "",
    "unit_tests": "",
    "test_plan": "",
    "live_test_notes": "",
    "readme_content": "",
    "suggested_commit_message": "",
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


def _input(**overrides: object) -> QAInput:
    base: Dict[str, object] = {
        "code": "def add(a, b):\n    return a + b",
        "language": "python",
        "task_description": "Implement a simple add function",
    }
    base.update(overrides)
    return QAInput(**base)  # type: ignore[arg-type]


def test_identical_review_hits_cache_and_skips_llm_call() -> None:
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = QAExpertAgent(client)

    first = agent.run(_input())
    second = agent.run(_input())

    assert client.calls == 1
    assert first.model_dump() == second.model_dump()


def test_changed_code_busts_cache() -> None:
    """A reviewed-file byte change naturally busts the key -- no explicit
    invalidation logic needed."""
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = QAExpertAgent(client)

    agent.run(_input())
    agent.run(_input(code="def add(a, b):\n    return b + a"))

    assert client.calls == 2


def test_redis_unavailable_falls_back_to_memory_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Redis-unreachable configuration falls back to an in-process cache
    that still produces correct (and still cache-capable) results."""
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(factory_mod, "_build_redis_client", lambda: None)
    reset_shared_cache_state()
    try:
        assert isinstance(get_shared_cache(agent_mod._review_cache_namespace()), MemoryBackend)

        client = _CountingClient(_CLEAN_RESPONSE)
        agent = QAExpertAgent(client)
        first = agent.run(_input())
        second = agent.run(_input())

        assert client.calls == 1
        assert first.model_dump() == second.model_dump()
    finally:
        reset_shared_cache_state()


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
            # blow up while agent_mod.get_shared_cache is still monkeypatched.
            pass

    monkeypatch.setattr(agent_mod, "get_shared_cache", lambda namespace: _RaisingCache())

    client = _CountingClient(_CLEAN_RESPONSE)
    agent = QAExpertAgent(client)
    result = agent.run(_input())

    assert isinstance(result, QAOutput)
    assert result.approved is True
    assert client.calls == 1


def test_unapproved_result_is_still_cached() -> None:
    """Unlike code-review's submission short-circuit, every genuine outcome
    is cached regardless of ``approved`` -- QAExpertAgent.run() is a single
    atomic call with no reduce phase to re-run on a retry."""
    response = dict(_CLEAN_RESPONSE)
    response["bugs_found"] = [{"severity": "critical", "description": "NPE in /auth"}]
    client = _CountingClient(response)
    agent = QAExpertAgent(client)

    first = agent.run(_input())
    second = agent.run(_input())

    assert first.approved is False
    assert second.approved is False
    assert client.calls == 1


def test_fallback_result_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured-output failure must be retried for real next time, not
    frozen into the cache as a permanent 'no bugs' verdict."""

    class _RaisingAgent:
        def __call__(self, *a: object, **kw: object) -> object:
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_mod, "Agent", lambda *, model, system_prompt: _RaisingAgent())

    agent = QAExpertAgent(DummyLLMClient())
    input_data = _input()
    result = agent.run(input_data)
    assert result.approved is False

    key = agent_mod._review_cache_key(input_data, agent_mod._model_fingerprint(agent._model))
    cache = get_shared_cache(agent_mod._review_cache_namespace())
    assert cache.get(key) is None


def test_cache_disabled_via_env_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QA_REVIEW_CACHE_SIZE", "0")
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = QAExpertAgent(client)

    agent.run(_input())
    agent.run(_input())

    assert client.calls == 2
