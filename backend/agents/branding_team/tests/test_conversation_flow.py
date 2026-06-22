"""Unit tests for the conversation-flow helpers in ``branding_team.api.main``.

Covers the mission-fingerprint short-circuit in ``_run_orchestrator_if_ready``
without needing a live LLM (the orchestrator is monkeypatched).
"""

from __future__ import annotations

import pytest

import branding_team.api.main as main
from branding_team.models import BrandingMission, BrandPhase, TeamOutput, WorkflowStatus
from branding_team.tests._fake_postgres import install_fake_postgres


@pytest.fixture(autouse=True)
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


def _ready_mission() -> BrandingMission:
    return BrandingMission(
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


def test_run_orchestrator_reuses_output_when_mission_unchanged(monkeypatch) -> None:
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


def test_run_orchestrator_returns_none_when_not_ready(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_run(**kwargs):
        calls["n"] += 1
        return _output()

    monkeypatch.setattr(main.orchestrator, "run", fake_run)
    incomplete = BrandingMission(
        company_name="Acme", company_description="To be discussed.", target_audience="TBD"
    )
    assert main._run_orchestrator_if_ready(incomplete, None, None) is None
    assert calls["n"] == 0
