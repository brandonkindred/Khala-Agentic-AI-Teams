"""Unit tests for the orchestrator."""

import os
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

    mock_init_result = MagicMock()
    mock_init_result.success = True

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch("orchestrator._get_agents", return_value=mock_agents):
            with patch(
                "shared.command_runner.ensure_backend_project_initialized",
                return_value=mock_init_result,
            ):
                orchestrator.run_failed_tasks(job_id)

    paused_calls = [
        (jid, kw) for jid, kw in update_job_calls
        if kw.get("status") == "paused_llm_limit"
    ]
    assert len(paused_calls) >= 1
    assert paused_calls[-1][1]["error"] == OLLAMA_WEEKLY_LIMIT_MESSAGE


def test_run_orchestrator_fails_job_when_planning_raises_no_fallback(tmp_path: Path) -> None:
    """When planning-v2 workflow fails (success=False), job fails with planning error."""
    (tmp_path / "initial_spec.md").write_text("# Test App\n\nBuild a todo app.", encoding="utf-8")
    job_id = "test-planning-fail"
    update_job_calls = []

    def capture_update_job(jid, **kwargs):
        update_job_calls.append((jid, kwargs))

    mock_p2_result = MagicMock()
    mock_p2_result.success = False
    mock_p2_result.failure_reason = "Planning-v2 workflow did not complete."

    mock_arch = MagicMock()
    arch_inputs_received = []

    def capture_arch_run(input_data):
        arch_inputs_received.append(input_data)
        return MagicMock(architecture=SystemArchitecture(overview="Mock architecture"))

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
    mock_tech_lead.llm.get_max_context_tokens.return_value = 262144

    mock_agents = {
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

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch("orchestrator._get_agents", return_value=mock_agents):
            with patch(
                "spec_parser.parse_spec_with_llm",
                return_value=ProductRequirements(
                    title="Test App",
                    description="Build a todo app",
                    acceptance_criteria=[],
                    constraints=[],
                ),
            ):
                with patch("planning_v2_team.PlanningV2TeamLead") as MockP2Lead:
                    mock_lead_instance = MagicMock()
                    mock_lead_instance.run_workflow.return_value = mock_p2_result
                    MockP2Lead.return_value = mock_lead_instance
                    orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [(jid, kw) for jid, kw in update_job_calls if kw.get("status") == "failed"]
    assert len(failed_calls) >= 1
    assert "planning" in failed_calls[0][1].get("error", "").lower()
    assert len(arch_inputs_received) == 0


def test_run_orchestrator_fails_job_when_project_planning_raises(tmp_path: Path) -> None:
    """When planning-v2 workflow fails (success=False), job is marked failed."""
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    job_id = "test-planning-total-fail"
    update_job_calls = []

    def capture_update_job(jid, **kwargs):
        update_job_calls.append((jid, kwargs))

    mock_p2_result = MagicMock()
    mock_p2_result.success = False
    mock_p2_result.failure_reason = "Planning failed"

    mock_agents = {
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
                with patch("planning_v2_team.PlanningV2TeamLead") as MockP2Lead:
                    mock_lead_instance = MagicMock()
                    mock_lead_instance.run_workflow.return_value = mock_p2_result
                    MockP2Lead.return_value = mock_lead_instance
                    orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [
        (jid, kw) for jid, kw in update_job_calls
        if kw.get("status") == "failed"
    ]
    assert len(failed_calls) >= 1
    assert "planning" in failed_calls[0][1].get("error", "").lower()
