"""Tests for the shared orchestrator singleton in ``core``."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from personal_assistant_team import core


@pytest.fixture(autouse=True)
def _reset_singleton():
    core.reset_orchestrator()
    yield
    core.reset_orchestrator()


class _FakeOrchestrator:
    def __init__(self, llm, credential_store, profile_store):
        self.llm = llm
        self.credential_store = credential_store
        self.profile_store = profile_store


def test_get_orchestrator_caches(monkeypatch):
    built: list = []

    def _factory(llm, credential_store, profile_store):
        built.append(1)
        return _FakeOrchestrator(llm, credential_store, profile_store)

    monkeypatch.setattr(
        "personal_assistant_team.orchestrator.agent.PersonalAssistantOrchestrator", _factory
    )

    first = core.get_orchestrator()
    second = core.get_orchestrator()

    assert first is second
    assert len(built) == 1
    # It wired the lazy LLM + shared stores.
    assert first.llm is not None
    assert first.credential_store is not None
    assert first.profile_store is not None


def test_reset_orchestrator_rebuilds(monkeypatch):
    monkeypatch.setattr(
        "personal_assistant_team.orchestrator.agent.PersonalAssistantOrchestrator",
        _FakeOrchestrator,
    )

    first = core.get_orchestrator()
    core.reset_orchestrator()
    second = core.get_orchestrator()

    assert first is not second


def test_get_orchestrator_concurrent_callers_get_same_instance(monkeypatch):
    # Regression guard for the double-checked-locking singleton: concurrent
    # first-callers (thread-mode dispatch racing a Temporal activity, the
    # exact scenario this module's docstring exists to solve) must all
    # observe the exact same instance, never each constructing their own.
    built: list = []

    def _factory(llm, credential_store, profile_store):
        built.append(1)
        return _FakeOrchestrator(llm, credential_store, profile_store)

    monkeypatch.setattr(
        "personal_assistant_team.orchestrator.agent.PersonalAssistantOrchestrator", _factory
    )

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: core.get_orchestrator(), range(32)))

    assert len({id(r) for r in results}) == 1
    assert len(built) == 1
