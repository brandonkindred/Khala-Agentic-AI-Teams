"""Tests for ``PersonalAssistantOrchestrator``'s profile-agent cache and
profile-update application."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..orchestrator.agent import PersonalAssistantOrchestrator
from ..orchestrator.models import OrchestratorRequest
from ..user_profile_agent.models import ExtractedPreference, ProfileExtractionResult


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


def test_check_for_profile_updates_calls_the_public_apply_preference(monkeypatch):
    # Regression test: _check_for_profile_updates must call UserProfileAgent's
    # PUBLIC apply_preference, not reach into a private method of another
    # class (an encapsulation violation the private name previously invited).
    orch = _orchestrator()
    profile_agent = orch._get_profile_agent("user-1")

    pref = ExtractedPreference(category="dietary", field="likes", value="oat milk", confidence=0.9)
    monkeypatch.setattr(
        profile_agent,
        "extract_preferences",
        lambda text: ProfileExtractionResult(extracted_info=[pref]),
    )
    assert not hasattr(profile_agent, "_apply_preference")

    applied_calls = []
    monkeypatch.setattr(profile_agent, "apply_preference", applied_calls.append)

    request = OrchestratorRequest(user_id="user-1", message="I love oat milk")
    result = orch._check_for_profile_updates(request)

    assert applied_calls == [pref]
    assert result == [pref.model_dump()]
