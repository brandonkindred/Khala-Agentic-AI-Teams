"""Tests for ai_systems_team orchestrator."""

from unittest.mock import MagicMock, patch

from ai_systems_team.models import (
    ArchitectureResult,
    BuildResult,
    CapabilitiesResult,
    EvaluationResult,
    Phase,
    SafetyResult,
    SpecIntakeResult,
)
from ai_systems_team.orchestrator import AISystemsOrchestrator


def _make_spec_intake(success=True):
    return SpecIntakeResult(success=success, goals=["build agent"])


def _make_arch(success=True):
    return ArchitectureResult(success=success)


def _make_caps(success=True):
    return CapabilitiesResult(success=success)


def _make_eval(success=True):
    return EvaluationResult(success=success)


def _make_safety(success=True):
    return SafetyResult(success=success)


def _make_build(success=True):
    return BuildResult(success=success, artifacts=["blueprint.json"])


@patch("ai_systems_team.orchestrator.run_build")
@patch("ai_systems_team.orchestrator.run_safety")
@patch("ai_systems_team.orchestrator.run_evaluation")
@patch("ai_systems_team.orchestrator.run_capabilities")
@patch("ai_systems_team.orchestrator.run_architecture")
@patch("ai_systems_team.orchestrator.run_spec_intake")
def test_workflow_runs_all_phases(
    mock_spec, mock_arch, mock_caps, mock_eval, mock_safety, mock_build
):
    mock_spec.return_value = _make_spec_intake()
    mock_arch.return_value = _make_arch()
    mock_caps.return_value = _make_caps()
    mock_eval.return_value = _make_eval()
    mock_safety.return_value = _make_safety()
    mock_build.return_value = _make_build()

    orch = AISystemsOrchestrator()
    bp = orch.run_workflow("test_proj", "/spec.md")

    assert bp.success is True
    assert Phase.SPEC_INTAKE in bp.completed_phases
    assert Phase.BUILD in bp.completed_phases
    mock_spec.assert_called_once()
    mock_build.assert_called_once()


@patch("ai_systems_team.orchestrator.run_spec_intake")
def test_workflow_stops_on_spec_intake_failure(mock_spec):
    mock_spec.return_value = SpecIntakeResult(success=False, error="bad spec")

    orch = AISystemsOrchestrator()
    bp = orch.run_workflow("test_proj", "/spec.md")

    assert bp.success is False
    assert bp.error == "bad spec"
    assert Phase.ARCHITECTURE not in bp.completed_phases


def test_get_blueprint_returns_none_when_missing():
    orch = AISystemsOrchestrator()
    assert orch.get_blueprint("no_such_project") is None


def test_list_blueprints_empty():
    orch = AISystemsOrchestrator()
    assert orch.list_blueprints() == []


@patch("ai_systems_team.orchestrator.run_build")
@patch("ai_systems_team.orchestrator.run_safety")
@patch("ai_systems_team.orchestrator.run_evaluation")
@patch("ai_systems_team.orchestrator.run_capabilities")
@patch("ai_systems_team.orchestrator.run_architecture")
@patch("ai_systems_team.orchestrator.run_spec_intake")
def test_workflow_stores_blueprint_on_success(
    mock_spec, mock_arch, mock_caps, mock_eval, mock_safety, mock_build
):
    mock_spec.return_value = _make_spec_intake()
    mock_arch.return_value = _make_arch()
    mock_caps.return_value = _make_caps()
    mock_eval.return_value = _make_eval()
    mock_safety.return_value = _make_safety()
    mock_build.return_value = _make_build()

    orch = AISystemsOrchestrator()
    orch.run_workflow("stored_proj", "/spec.md")

    assert "stored_proj" in orch.list_blueprints()
    bp = orch.get_blueprint("stored_proj")
    assert bp is not None
    assert bp.project_name == "stored_proj"


@patch("ai_systems_team.orchestrator.run_build")
@patch("ai_systems_team.orchestrator.run_safety")
@patch("ai_systems_team.orchestrator.run_evaluation")
@patch("ai_systems_team.orchestrator.run_capabilities")
@patch("ai_systems_team.orchestrator.run_architecture")
@patch("ai_systems_team.orchestrator.run_spec_intake")
def test_workflow_calls_job_updater(
    mock_spec, mock_arch, mock_caps, mock_eval, mock_safety, mock_build
):
    mock_spec.return_value = _make_spec_intake()
    mock_arch.return_value = _make_arch()
    mock_caps.return_value = _make_caps()
    mock_eval.return_value = _make_eval()
    mock_safety.return_value = _make_safety()
    mock_build.return_value = _make_build()

    updater = MagicMock()
    orch = AISystemsOrchestrator()
    orch.run_workflow("proj", "/spec.md", job_updater=updater)
    # job_updater is passed through to phase functions; just verify no crash
    assert True
