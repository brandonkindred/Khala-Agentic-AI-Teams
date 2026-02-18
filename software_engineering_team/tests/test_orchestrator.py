"""Unit tests for the orchestrator."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from shared.command_runner import CommandResult
from shared.llm import LLMRateLimitError, OLLAMA_WEEKLY_LIMIT_MESSAGE
from shared.models import (
    ProductRequirements,
    SystemArchitecture,
    Task,
    TaskAssignment,
    TaskType,
)
import orchestrator


def test_run_build_verification_appends_fix_line_when_pytest_fails_with_test_error_handlers(
    tmp_path: Path,
) -> None:
    """When pytest fails and summary contains test_error_handlers, returned error includes FIX line."""
    # Set up backend dir with Python files and tests so pytest path is taken.
    # backend_dir = tmp_path when repo has .py files; tests_dir = tmp_path / "tests"
    (tmp_path / "main.py").write_text("x = 1", encoding="utf-8")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_foo.py").write_text("def test_foo(): pass", encoding="utf-8")

    failure_summary = (
        "= FAILURES =\n"
        "________________________ test_generic_exception_handler ________________________\n"
        "tests/test_error_handlers.py:108: in test_generic_exception_handler\n"
        "    response = client.get(\"/test-generic-error\")"
    )
    mock_result = CommandResult(
        success=False,
        exit_code=1,
        stdout=failure_summary,
        stderr="",
    )

    with patch(
        "shared.command_runner.run_python_syntax_check",
        return_value=CommandResult(True, 0, "", ""),
    ):
        with patch("shared.command_runner.run_pytest", return_value=mock_result):
            ok, error_output = orchestrator._run_build_verification(
                tmp_path, "backend", "task-1"
            )

    assert ok is False
    assert "FIX: Preserve the /test-generic-error route" in error_output
    assert "JSONResponse" in error_output
    assert "do not re-raise" in error_output


def test_run_orchestrator_pauses_on_llm_rate_limit_in_spec_parsing(
    tmp_path: Path,
) -> None:
    """When parse_spec_with_llm raises LLMRateLimitError, job is paused with paused_llm_limit."""
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec content.", encoding="utf-8")
    job_id = "test-job-llm-limit"
    update_job_calls = []

    def capture_update_job(jid, **kwargs):
        update_job_calls.append((jid, kwargs))

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch(
            "spec_parser.parse_spec_with_llm",
            side_effect=LLMRateLimitError("429 rate limited", status_code=429),
        ):
            orchestrator.run_orchestrator(job_id, str(tmp_path))

    paused_calls = [
        (jid, kw) for jid, kw in update_job_calls
        if kw.get("status") == "paused_llm_limit"
    ]
    assert len(paused_calls) >= 1
    assert paused_calls[0][1]["error"] == OLLAMA_WEEKLY_LIMIT_MESSAGE


def test_run_failed_tasks_pauses_on_llm_rate_limit(tmp_path: Path) -> None:
    """When backend run_workflow raises LLMRateLimitError during retry, job is paused with paused_llm_limit."""
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    (backend_dir / ".git").mkdir()
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()

    job_id = "test-retry-llm-limit"
    task = Task(
        id="backend-task-1",
        type=TaskType.BACKEND,
        title="Backend task",
        description="Implement API",
        assignee="backend",
    )
    task_data = task.model_dump() if hasattr(task, "model_dump") else task.dict()
    from shared.job_store import create_job, update_job
    create_job(job_id, str(tmp_path))
    update_job(
        job_id,
        failed_tasks=[{"task_id": "backend-task-1", "reason": "previous fail", "title": "Backend task"}],
        _all_tasks={"backend-task-1": task_data},
        _architecture_overview="API + frontend",
        _spec_content="# Test\n\nSpec.",
    )

    update_job_calls = []

    def capture_update_job(jid, cache_dir=None, **kwargs):
        update_job_calls.append((jid, kwargs))

    mock_backend = MagicMock()
    mock_backend.run_workflow.side_effect = LLMRateLimitError(
        "429 rate limited", status_code=429
    )
    mock_git_setup = MagicMock()
    mock_git_setup.run.return_value = MagicMock(success=True)
    mock_agents = {
        "backend": mock_backend,
        "frontend": MagicMock(),
        "git_setup": mock_git_setup,
        "tech_lead": MagicMock(),
        "devops": MagicMock(),
        "qa": MagicMock(),
        "security": MagicMock(),
        "dbc_comments": MagicMock(),
        "code_review": MagicMock(),
        "accessibility": MagicMock(),
    }

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch("orchestrator._get_agents", return_value=mock_agents):
            orchestrator.run_failed_tasks(job_id)

    paused_calls = [
        (jid, kw) for jid, kw in update_job_calls
        if kw.get("status") == "paused_llm_limit"
    ]
    assert len(paused_calls) >= 1
    assert paused_calls[-1][1]["error"] == OLLAMA_WEEKLY_LIMIT_MESSAGE


def test_run_orchestrator_uses_fallback_overview_when_planning_raises(tmp_path: Path) -> None:
    """When project planning raises, fallback overview is used and architecture receives non-null project_overview."""
    (tmp_path / "initial_spec.md").write_text("# Test App\n\nBuild a todo app.", encoding="utf-8")
    job_id = "test-planning-fallback"
    arch_inputs_received = []

    def capture_arch_run(input_data):
        arch_inputs_received.append(input_data)
        return MagicMock(architecture=SystemArchitecture(overview="Mock architecture"))

    mock_project_planning = MagicMock()
    mock_project_planning.run.side_effect = Exception("LLM failed")

    mock_arch = MagicMock()
    mock_arch.run.side_effect = capture_arch_run

    one_task = Task(
        id="t1",
        type=TaskType.BACKEND,
        title="Backend task",
        assignee="backend",
    )
    mock_tech_lead = MagicMock()
    mock_tech_lead.run.return_value = MagicMock(
        spec_clarification_needed=False,
        assignment=TaskAssignment(tasks=[one_task], execution_order=["t1"]),
        summary="",
        requirement_task_mapping=[],
    )

    mock_agents = {
        "project_planning": mock_project_planning,
        "architecture": mock_arch,
        "tech_lead": mock_tech_lead,
        "devops": MagicMock(),
        "backend": MagicMock(),
        "frontend": MagicMock(),
        "git_setup": MagicMock(),
        "integration": MagicMock(),
        "acceptance_verifier": MagicMock(),
        "qa": MagicMock(),
        "security": MagicMock(),
        "accessibility": MagicMock(),
        "code_review": MagicMock(),
        "dbc_comments": MagicMock(),
        "documentation": MagicMock(),
    }

    with patch("orchestrator.update_job"):
        with patch("orchestrator._get_agents", return_value=mock_agents):
            with patch(
                "planning_team.planning_review.check_tasks_architecture_alignment",
                return_value=(True, []),
            ):
                with patch(
                    "planning_team.planning_review.check_spec_conformance",
                    return_value=(True, []),
                ):
                    with patch(
                        "spec_parser.parse_spec_with_llm",
                        return_value=ProductRequirements(
                            title="Test App",
                            description="Build a todo app",
                            acceptance_criteria=[],
                            constraints=[],
                        ),
                    ):
                        with patch("spec_parser.parse_spec_heuristic", return_value=ProductRequirements(
                            title="Test App",
                            description="Build a todo app",
                            acceptance_criteria=[],
                            constraints=[],
                        )):
                            orchestrator.run_orchestrator(job_id, str(tmp_path))

    assert len(arch_inputs_received) == 1
    assert arch_inputs_received[0].project_overview is not None
    assert isinstance(arch_inputs_received[0].project_overview, dict)
    assert "primary_goal" in arch_inputs_received[0].project_overview
    assert "delivery_strategy" in arch_inputs_received[0].project_overview


def test_run_orchestrator_fails_job_when_planning_and_fallback_both_fail(tmp_path: Path) -> None:
    """When both project planning and fallback raise, job is marked failed."""
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    job_id = "test-planning-total-fail"
    update_job_calls = []

    def capture_update_job(jid, **kwargs):
        update_job_calls.append((jid, kwargs))

    mock_project_planning = MagicMock()
    mock_project_planning.run.side_effect = Exception("LLM failed")

    mock_agents = {
        "project_planning": mock_project_planning,
        "architecture": MagicMock(),
        "tech_lead": MagicMock(),
        "devops": MagicMock(),
        "backend": MagicMock(),
        "frontend": MagicMock(),
        "git_setup": MagicMock(),
        "integration": MagicMock(),
        "acceptance_verifier": MagicMock(),
        "qa": MagicMock(),
        "security": MagicMock(),
        "accessibility": MagicMock(),
        "code_review": MagicMock(),
        "dbc_comments": MagicMock(),
        "documentation": MagicMock(),
    }

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch("orchestrator._get_agents", return_value=mock_agents):
            with patch(
                "spec_parser.parse_spec_with_llm",
                return_value=ProductRequirements(
                    title="Test",
                    description="Desc",
                    acceptance_criteria=[],
                    constraints=[],
                ),
            ):
                with patch(
                    "planning_team.project_planning_agent.models.build_fallback_overview_from_requirements",
                    side_effect=Exception("Fallback failed"),
                ):
                    orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [
        (jid, kw) for jid, kw in update_job_calls
        if kw.get("status") == "failed"
    ]
    assert len(failed_calls) >= 1
    assert "fallback" in failed_calls[0][1].get("error", "").lower() or "planning" in failed_calls[0][1].get("error", "").lower()
