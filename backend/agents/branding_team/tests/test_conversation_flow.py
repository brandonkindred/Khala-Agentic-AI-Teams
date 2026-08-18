"""Unit tests for the conversation-flow helpers in ``branding_team.api.main``.

Covers the mission short-circuit in ``_run_orchestrator_if_ready`` without
needing a live LLM (the orchestrator is monkeypatched).

These deliberately exercise the private ``_run_orchestrator_if_ready`` directly:
the short-circuit is a self-contained, deterministic decision (run vs. reuse the
prior output) whose only observable effect through the chat endpoints is "was
the orchestrator invoked". Driving it through the endpoint would require a live
(or heavily mocked) LLM assistant and the full request stack to assert that one
internal call count, which tests far more than this unit and is covered
separately by the integration tests. The function's contract (inputs/return) is
stable, so testing it directly is the cheaper, more precise check.
"""

from __future__ import annotations

import pytest

import branding_team.api.main as main
from branding_team.models import BrandPhase, TeamOutput, WorkflowStatus
from branding_team.shared.phase_output_cache import PhaseOutputCache
from branding_team.tests.conftest import make_mission


def _ready_mission():
    return make_mission(
        company_name="Acme",
        company_description="A real company description that is long enough.",
        target_audience="developers",
    )


def _output() -> TeamOutput:
    return TeamOutput(
        status=WorkflowStatus.NEEDS_HUMAN_DECISION,
        mission_summary="draft",
        current_phase=BrandPhase.STRATEGIC_CORE,
    )


def test_run_orchestrator_reuses_output_when_mission_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged mission reuses the prior output; a changed mission re-runs."""
    calls = {"n": 0}
    sentinel = _output()

    def fake_run(**kwargs):
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    mission = _ready_mission()

    # First call (no previous) runs the pipeline.
    assert main._run_orchestrator_if_ready(mission, None, None) is sentinel
    assert calls["n"] == 1

    # Unchanged mission + a previous output → reuse, no new run.
    assert main._run_orchestrator_if_ready(mission, mission, sentinel) is sentinel
    assert calls["n"] == 1

    # A changed mission → run again.
    changed = mission.model_copy(update={"target_audience": "designers"})
    main._run_orchestrator_if_ready(changed, mission, sentinel)
    assert calls["n"] == 2


def test_run_orchestrator_returns_none_when_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mission with placeholder values for required fields short-circuits to None without running."""
    calls = {"n": 0}

    def fake_run(**kwargs):
        calls["n"] += 1
        return _output()

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    incomplete = make_mission(
        company_name="Acme", company_description="To be discussed.", target_audience="TBD"
    )
    assert main._run_orchestrator_if_ready(incomplete, None, None) is None
    assert calls["n"] == 0


def test_run_orchestrator_forwards_phase_cache_to_orchestrator_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplied ``phase_cache`` reaches ``orchestrator.run`` unchanged (Story 2c Step 2)."""
    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return _output()

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    mission = _ready_mission()
    cache = PhaseOutputCache()

    main._run_orchestrator_if_ready(mission, None, None, phase_cache=cache)

    assert captured["phase_cache"] is cache


def test_run_orchestrator_short_circuit_ignores_phase_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole-mission short-circuit stays outermost even when a phase_cache is supplied."""
    calls = {"n": 0}
    sentinel = _output()

    def fake_run(**kwargs):
        calls["n"] += 1
        return sentinel

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    mission = _ready_mission()
    cache = PhaseOutputCache()

    result = main._run_orchestrator_if_ready(mission, mission, sentinel, phase_cache=cache)

    assert result is sentinel
    assert calls["n"] == 0
