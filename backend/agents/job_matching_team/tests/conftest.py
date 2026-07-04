"""Shared fixtures/helpers for job matching tests."""

from __future__ import annotations

from typing import Any, Callable, Dict

import pytest


class ScriptedLLM:
    """Minimal stand-in for ``llm_service.LLMClient``.

    ``handler(prompt, system_prompt)`` returns the dict that ``complete_json``
    should yield, letting each test script deterministic model output.
    """

    def __init__(self, handler: Callable[[str, str], Dict[str, Any]]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str]] = []

    def complete_json(
        self, prompt: str, *, temperature: float = 0.0, system_prompt: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        self.calls.append((prompt, system_prompt or ""))
        return self._handler(prompt, system_prompt or "")

    def complete(self, prompt: str, **kwargs: Any) -> str:  # pragma: no cover - unused
        return ""


@pytest.fixture
def scripted_llm() -> Callable[[Callable[[str, str], Dict[str, Any]]], ScriptedLLM]:
    def _make(handler: Callable[[str, str], Dict[str, Any]]) -> ScriptedLLM:
        return ScriptedLLM(handler)

    return _make


@pytest.fixture(autouse=True)
def _hermetic_profile_env(monkeypatch):
    """Keep every job-matching test independent of the developer's environment.

    The profile loader resolves through env-driven sources (``POSTGRES_HOST``
    career section, ``JOB_SEEKER_PROFILE_PATH``, ``AGENT_CACHE``) and flips
    fallbacks into raises under ``JOB_SEEKER_PROFILE_STRICT``. Any test that
    touches the loader — directly or through the API's ``GET /profile`` —
    would otherwise read the developer's saved career profile (and
    ``get_profile`` would lazily INSERT rows into the shared dev database as
    a test side effect). Tests that need one of these sources re-enable it
    explicitly with ``monkeypatch.setenv``.
    """
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("JOB_SEEKER_PROFILE_PATH", raising=False)
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    monkeypatch.delenv("JOB_SEEKER_PROFILE_STRICT", raising=False)


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    """The loader memoizes the resolved profile; isolate tests from each other."""
    from job_matching_team.profile.loader import clear_cache

    clear_cache()
    yield
    clear_cache()
