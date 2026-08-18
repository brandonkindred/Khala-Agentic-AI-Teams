"""Tests for CybersecurityExpertAgent's shared review-result cache.

Mirrors ``test_qa_agent_cache.py``'s conventions for the analogous QA
cache: a ``_CountingClient`` counts LLM invocations so a hit (no call) can
be distinguished from a miss (a call). See ``security_agent/agent.py``'s
module-level comment for how this cache's key *shape* (whole-input hash)
and caching *policy* (every genuine outcome, not just approved ones) mirror
``qa_agent``'s review cache.

The cache itself (``shared.cache`` — Redis when configured, otherwise an
in-process store) is cleared around every test by the autouse
``_reset_security_review_cache`` fixture in ``conftest.py``, so tests do not
observe cross-test cache hits.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
import security_agent.agent as agent_mod
from security_agent import CybersecurityExpertAgent
from security_agent.models import SecurityInput, SecurityOutput

from llm_service.clients.dummy import DummyLLMClient
from shared.cache import MemoryBackend, get_shared_cache, reset_shared_cache_state
from shared.cache import factory as factory_mod

_CLEAN_RESPONSE: Dict[str, Any] = {
    "vulnerabilities": [],
    "summary": "looks fine",
    "remediations": [],
}


class _CountingClient(DummyLLMClient):
    """Returns a fixed canned response; counts ``complete_json`` calls.

    ``run_single_shot_review``'s schema-validated branch ultimately calls
    the client's ``complete_json`` (via ``llm_service.generate_structured``
    -> ``complete_validated``) — see ``test_security_agent.py``'s
    ``_LyingClient``/``_MediumOnlyClient``/etc. for the same override
    pattern proven against this agent's actual call path.
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


def _input(**overrides: object) -> SecurityInput:
    base: Dict[str, object] = {
        "code": "import os\n\ndef run(cmd):\n    os.system(cmd)",
        "language": "python",
        "task_description": "Security review of command runner",
    }
    base.update(overrides)
    return SecurityInput(**base)  # type: ignore[arg-type]


def test_identical_review_hits_cache_and_skips_llm_call() -> None:
    """A byte-identical ``SecurityInput`` resubmission hits the cache and skips the LLM call."""
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = CybersecurityExpertAgent(client)

    first = agent.run(_input())
    second = agent.run(_input())

    assert client.calls == 1
    assert first.model_dump() == second.model_dump()


def test_changed_code_busts_cache() -> None:
    """A reviewed-file byte change naturally busts the key -- no explicit
    invalidation logic needed."""
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = CybersecurityExpertAgent(client)

    agent.run(_input())
    agent.run(_input(code="import os\n\ndef run(cmd):\n    os.system(cmd + ';')"))

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
        agent = CybersecurityExpertAgent(client)
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
    agent = CybersecurityExpertAgent(client)
    result = agent.run(_input())

    assert isinstance(result, SecurityOutput)
    assert result.approved is True
    assert client.calls == 1


def test_result_with_vulnerabilities_is_still_cached() -> None:
    """Every genuine outcome is cached regardless of ``approved`` --
    CybersecurityExpertAgent.run() is a single atomic call with no reduce
    phase to re-run on a retry.

    Asserts on ``vulnerabilities`` (the actual precondition: a non-clean
    result) rather than the derived ``approved`` flag -- that derivation is
    ``test_security_agent.py``'s concern, not this cache test's.
    """
    response = dict(_CLEAN_RESPONSE)
    response["vulnerabilities"] = [
        {
            "severity": "critical",
            "category": "injection",
            "description": "Command injection in run()",
            "location": "run:3",
            "recommendation": "Use subprocess with shell=False",
        }
    ]
    client = _CountingClient(response)
    agent = CybersecurityExpertAgent(client)

    first = agent.run(_input())
    second = agent.run(_input())

    assert first.vulnerabilities  # not a clean result
    assert client.calls == 1
    assert second.model_dump() == first.model_dump()


def test_fallback_result_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured-output failure must be retried for real next time, not
    frozen into the cache as a permanent 'no vulnerabilities' verdict."""

    def _raise(*a: object, **kw: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_mod, "run_single_shot_review", _raise)

    agent = CybersecurityExpertAgent(DummyLLMClient())
    input_data = _input()
    result = agent.run(input_data)
    assert result.approved is False
    assert "Security analysis failed" in result.summary

    key = agent_mod._review_cache_key(input_data, agent_mod._security_model_fingerprint(agent.llm))
    cache = get_shared_cache(agent_mod._review_cache_namespace())
    assert cache.get(key) is None


def test_cache_disabled_via_env_is_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Setting ``SECURITY_REVIEW_CACHE_SIZE=0`` disables the cache; every call invokes the model."""
    monkeypatch.setenv("SECURITY_REVIEW_CACHE_SIZE", "0")
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = CybersecurityExpertAgent(client)

    agent.run(_input())
    agent.run(_input())

    assert client.calls == 2


def test_clear_review_cache_falls_open_on_backend_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``clear_review_cache()`` never raises, even when the backend does."""

    class _RaisingCache:
        def clear(self) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(agent_mod, "get_shared_cache", lambda namespace: _RaisingCache())

    agent_mod.clear_review_cache()  # must not raise


def test_model_fingerprint_resolution_failure_falls_back_to_type_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model-resolution failure falls back to the client's type name, never raises."""

    def _raise(*a: object, **kw: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(agent_mod, "resolve_strands_model", _raise)

    client = DummyLLMClient()
    assert agent_mod._security_model_fingerprint(client) == type(client).__name__


def test_corrupt_cache_entry_treated_as_miss() -> None:
    """A cache entry that fails to validate against ``SecurityOutput`` is
    treated as a miss (and dropped), not surfaced as a broken review."""
    client = _CountingClient(_CLEAN_RESPONSE)
    agent = CybersecurityExpertAgent(client)
    input_data = _input()

    key = agent_mod._review_cache_key(input_data, agent_mod._security_model_fingerprint(agent.llm))
    cache = get_shared_cache(agent_mod._review_cache_namespace())
    cache.set(key, b"not valid json", max_entries=agent_mod._review_cache_size())

    result = agent.run(input_data)

    assert isinstance(result, SecurityOutput)
    assert client.calls == 1
    assert cache.get(key) is not None  # the genuine result was written back


def test_corrupt_cache_entry_delete_failure_falls_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache whose ``delete`` also fails after a corrupt entry still falls
    open to a genuine review rather than aborting."""

    class _CorruptThenRaisingCache:
        def get(self, key: str) -> bytes:
            return b"not valid json"

        def set(self, key: str, value: bytes, *, max_entries: int) -> None:
            pass

        def delete(self, key: str) -> None:
            raise RuntimeError("boom")

        def clear(self) -> None:
            pass

    monkeypatch.setattr(agent_mod, "get_shared_cache", lambda namespace: _CorruptThenRaisingCache())

    client = _CountingClient(_CLEAN_RESPONSE)
    agent = CybersecurityExpertAgent(client)
    result = agent.run(_input())

    assert isinstance(result, SecurityOutput)
    assert client.calls == 1
