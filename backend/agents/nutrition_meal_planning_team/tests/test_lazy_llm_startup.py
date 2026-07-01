"""Regression test: the nutrition container must not crash when no LLM
provider is configured.

``NutritionMealPlanningOrchestrator.__init__`` used to resolve its Strands
model eagerly via ``get_strands_model(...)``, which raises
``LLMNotConfiguredError`` when the Postgres-backed provider list is empty.
Because ``api/main.py`` built the orchestrator as a module-level singleton,
that crashed the whole process at import time before ``/health`` (or the
``/llm-config`` setup flow) could ever serve a request. ``get_orchestrator()``
now builds the orchestrator lazily with ``get_strands_model(..., lazy=True)``,
so construction must succeed regardless of provider configuration — only an
actual LLM call should fail.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip(
    "strands", reason="orchestrator requires strands-agents (install via make install-dev)"
)

from llm_service import LLMNotConfiguredError, get_strands_model  # noqa: E402
from nutrition_meal_planning_team.orchestrator.agent import (  # noqa: E402
    NutritionMealPlanningOrchestrator,
)


def _build_orchestrator_with_unconfigured_llm(monkeypatch) -> NutritionMealPlanningOrchestrator:
    """Real (non-``object.__new__``) orchestrator whose Strands model is lazy
    and whose backing provider is guaranteed unconfigured, without touching
    Postgres (the stores are mocked)."""
    import llm_service.strands_provider as sp

    def _raise(*_args, **_kwargs):
        raise LLMNotConfiguredError("No LLM provider is configured.")

    monkeypatch.setattr(sp, "get_client", _raise)

    return NutritionMealPlanningOrchestrator(
        profile_store=MagicMock(),
        meal_feedback_store=MagicMock(),
        nutrition_plan_store=MagicMock(),
        guardrail_audit_store=MagicMock(),
        llm_model=get_strands_model("nutrition_meal_planning", lazy=True),
    )


def test_orchestrator_constructs_without_configured_llm(monkeypatch):
    """Construction must succeed even when the LLM provider list is empty —
    this is what previously crashed the container at import time."""
    orch = _build_orchestrator_with_unconfigured_llm(monkeypatch)
    assert orch.intake_agent is not None
    assert orch.meal_planning_agent is not None
    assert orch.chat_agent is not None


def test_intake_agent_falls_through_to_structural_merge_without_llm(monkeypatch):
    """An unconfigured provider must not crash a request: the intake agent's
    documented structural-merge fallback still produces a profile."""
    orch = _build_orchestrator_with_unconfigured_llm(monkeypatch)
    profile = orch.intake_agent.run("client-1", update=None, current_profile=None)
    assert profile.client_id == "client-1"
