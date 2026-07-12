"""Tests for ``PersonalAssistantOrchestrator._get_profile_agent``'s caching lock."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..orchestrator.agent import PersonalAssistantOrchestrator


class _StubLLM:
    def complete(self, prompt, **kwargs):
        return ""

    def complete_json(self, prompt, **kwargs):
        return {}

    def get_max_context_tokens(self) -> int:
        return 4096


def _orchestrator() -> PersonalAssistantOrchestrator:
    return PersonalAssistantOrchestrator(_StubLLM())


def test_get_profile_agent_caches_per_user():
    orch = _orchestrator()

    first = orch._get_profile_agent("user-1")
    second = orch._get_profile_agent("user-1")

    assert first is second


def test_get_profile_agent_distinct_per_user():
    orch = _orchestrator()

    a = orch._get_profile_agent("user-a")
    b = orch._get_profile_agent("user-b")

    assert a is not b


def test_get_profile_agent_concurrent_callers_get_same_instance():
    # Regression test for the check-then-set race: before the lock was added,
    # concurrent callers for the same user_id could each construct a distinct
    # UserProfileAgent and silently overwrite one another's cache entry. With
    # the lock, every concurrent caller must observe the exact same instance.
    orch = _orchestrator()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(orch._get_profile_agent, ["user-concurrent"] * 32))

    assert len({id(r) for r in results}) == 1
