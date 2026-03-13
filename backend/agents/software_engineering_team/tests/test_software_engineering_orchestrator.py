"""Unit tests for the orchestrator."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from software_engineering_team.shared.command_runner import CommandResult
from llm_service import LLMJsonParseError, LLMRateLimitError, OLLAMA_WEEKLY_LIMIT_MESSAGE
from software_engineering_team.shared.models import (
    ProductRequirements,
    SystemArchitecture,
    Task,
    TaskAssignment,
    TaskUpdate,
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
    from software_engineering_team.shared.job_store import create_job, update_job
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
    """When Planning V3 workflow fails (success=False), job fails with planning error."""
    (tmp_path / "initial_spec.md").write_text("# Test App\n\nBuild a todo app.", encoding="utf-8")
    job_id = "test-planning-fail"
    update_job_calls = []

    def capture_update_job(jid, **kwargs):
        update_job_calls.append((jid, kwargs))

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

    mock_pra_result = MagicMock()
    mock_pra_result.success = True
    mock_pra_result.final_spec_content = "# Test App\n\nBuild a todo app."
    mock_pra_result.iterations = 1
    mock_pra_agent = MagicMock()
    mock_pra_agent.run_workflow.return_value = mock_pra_result

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
                with patch("product_requirements_analysis_agent.ProductRequirementsAnalysisAgent", return_value=mock_pra_agent):
                    with patch("planning_v3_team.orchestrator.run_workflow") as mock_run_v3:
                        mock_run_v3.return_value = {"success": False, "failure_reason": "Planning failed"}
                        orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [(jid, kw) for jid, kw in update_job_calls if kw.get("status") == "failed"]
    assert len(failed_calls) >= 1
    assert "planning" in failed_calls[0][1].get("error", "").lower()
    assert len(arch_inputs_received) == 0


def test_run_orchestrator_fails_job_when_project_planning_raises(tmp_path: Path) -> None:
    """When Planning V3 workflow fails (success=False), job is marked failed."""
    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    job_id = "test-planning-total-fail"
    update_job_calls = []

    def capture_update_job(jid, **kwargs):
        update_job_calls.append((jid, kwargs))

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

    mock_pra_result = MagicMock()
    mock_pra_result.success = True
    mock_pra_result.final_spec_content = "# Test\n\nSpec."
    mock_pra_result.iterations = 1
    mock_pra_agent = MagicMock()
    mock_pra_agent.run_workflow.return_value = mock_pra_result

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
                with patch("product_requirements_analysis_agent.ProductRequirementsAnalysisAgent", return_value=mock_pra_agent):
                    with patch("planning_v3_team.orchestrator.run_workflow") as mock_run_v3:
                        mock_run_v3.return_value = {"success": False, "failure_reason": "Planning failed"}
                        orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [
        (jid, kw) for jid, kw in update_job_calls
        if kw.get("status") == "failed"
    ]
    assert len(failed_calls) >= 1
    assert "planning" in failed_calls[0][1].get("error", "").lower()


def test_frontend_json_parse_failure_triggers_tech_lead_review_for_task_breakdown(
    tmp_path: Path,
) -> None:
    """When frontend run_workflow raises LLMJsonParseError, _run_tech_lead_review is called to break task."""
    from software_engineering_team.shared.job_store import create_job, update_job

    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / ".git").mkdir()
    (frontend_dir / "package.json").write_text("{}", encoding="utf-8")
    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()

    job_id = "test-json-parse-fail"
    create_job(job_id, str(tmp_path))
    update_job(job_id, status="running")

    frontend_task_id = "frontend-task-1"
    task = Task(
        id=frontend_task_id,
        type=TaskType.FRONTEND,
        title="Frontend task",
        description="Implement UI",
        assignee="frontend",
    )
    all_tasks = {frontend_task_id: task}
    frontend_queue = [frontend_task_id]
    backend_queue = []
    completed = set()
    failed = {}
    completed_code_task_ids = []

    mock_frontend = MagicMock()
    mock_frontend.run_workflow.side_effect = LLMJsonParseError(
        "Could not parse structured JSON from LLM response.",
        error_kind="json_parse",
        response_preview="truncated...",
    )
    mock_init = MagicMock()
    mock_init.success = True
    mock_git_setup = MagicMock()
    mock_git_setup.run.return_value = MagicMock(success=True)
    mock_tech_lead = MagicMock()
    mock_tech_lead.review_progress.return_value = []
    mock_agents = {
        "frontend": mock_frontend,
        "backend": MagicMock(),
        "git_setup": mock_git_setup,
        "tech_lead": mock_tech_lead,
        "qa": MagicMock(),
        "security": MagicMock(),
        "accessibility": MagicMock(),
        "code_review": MagicMock(),
        "dbc_comments": MagicMock(),
        "acceptance_verifier": MagicMock(),
        "documentation": MagicMock(),
        "linting_tool_agent": None,
        "build_fix_specialist": None,
    }

    review_calls = []

    def capture_review(*args, **kwargs):
        review_calls.append(kwargs)
        if kwargs.get("task_update"):
            for nt in mock_tech_lead.review_progress.return_value:
                if kwargs.get("append_task_id_fn") and nt.id not in all_tasks:
                    all_tasks[nt.id] = nt
                    kwargs["append_task_id_fn"](nt.id)

    with patch(
        "shared.command_runner.ensure_frontend_project_initialized",
        return_value=mock_init,
    ):
        with patch("orchestrator._run_tech_lead_review", side_effect=capture_review):
            orchestrator._run_backend_frontend_workers(
                job_id=job_id,
                path=tmp_path,
                backend_dir=backend_dir,
                frontend_dir=frontend_dir,
                backend_queue=backend_queue,
                frontend_queue=frontend_queue,
                all_tasks=all_tasks,
                completed=completed,
                failed=failed,
                completed_code_task_ids=completed_code_task_ids,
                spec_content="# Test\n\nSpec.",
                architecture=MagicMock(overview="Mock arch"),
                agents=mock_agents,
                tech_lead=mock_tech_lead,
                total_tasks=1,
                is_retry=False,
            )

    assert frontend_task_id in failed
    assert len(review_calls) == 1
    task_update = review_calls[0]["task_update"]
    assert isinstance(task_update, TaskUpdate)
    assert task_update.task_id == frontend_task_id
    assert task_update.agent_type == "frontend"
    assert task_update.status == "failed"
    assert task_update.failure_class == "json_parse"
    assert "parse" in (task_update.failure_reason or "").lower() or "json" in (task_update.failure_reason or "").lower()


def test_run_orchestrator_invokes_coding_team_not_legacy_tech_lead_or_v2_workers(tmp_path: Path) -> None:
    """Main path: after Planning V3 and adapter, run_coding_team_orchestrator is called; Tech Lead and v2 workers are not."""
    from planning_v3_adapter import PlanningV2AdapterResult

    (tmp_path / "initial_spec.md").write_text("# Test\n\nSpec.", encoding="utf-8")
    job_id = "test-coding-team-path"
    update_job_calls = []

    def capture_update_job(jid, **kwargs):
        update_job_calls.append((jid, kwargs))

    mock_pra_result = MagicMock()
    mock_pra_result.success = True
    mock_pra_result.final_spec_content = "# Test\n\nSpec."
    mock_pra_result.iterations = 1
    mock_pra_agent = MagicMock()
    mock_pra_agent.run_workflow.return_value = mock_pra_result

    adapter_result = PlanningV2AdapterResult(
        requirements=ProductRequirements(
            title="Test",
            description="Desc",
            acceptance_criteria=[],
            constraints=[],
        ),
        project_overview={"goals": "Ship", "features_and_functionality_doc": "API"},
        open_questions=[],
        assumptions=[],
        hierarchy=None,
        final_spec_content="# Test\n\nSpec.",
        architecture_overview="Backend FastAPI; frontend Angular.",
    )

    coding_team_calls = []

    def capture_run_coding_team(jid, repo_path, plan_input, **kwargs):
        coding_team_calls.append({"job_id": jid, "repo_path": repo_path, "plan_input": plan_input, **kwargs})
        if kwargs.get("update_job_fn"):
            kwargs["update_job_fn"](status="completed", phase="completed")

    mock_agents = {
        "architecture": MagicMock(),
        "tech_lead": MagicMock(),
        "devops": MagicMock(),
        "backend": MagicMock(),
        "frontend": MagicMock(),
        "frontend_code_v2": MagicMock(),
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
                    "product_requirements_analysis_agent.ProductRequirementsAnalysisAgent",
                    return_value=mock_pra_agent,
                ):
                    with patch("planning_v3_team.orchestrator.run_workflow") as mock_run_v3:
                        mock_run_v3.return_value = {
                            "success": True,
                            "handoff_package": {"architecture_overview": "Backend FastAPI; frontend Angular."},
                            "failure_reason": None,
                        }
                        with patch(
                            "planning_v3_adapter.adapt_planning_v3_result",
                            return_value=adapter_result,
                        ):
                            with patch(
                                "coding_team.orchestrator.run_coding_team_orchestrator",
                                side_effect=capture_run_coding_team,
                            ):
                                orchestrator.run_orchestrator(job_id, str(tmp_path))

    assert len(coding_team_calls) == 1
    call = coding_team_calls[0]
    assert call["job_id"] == job_id
    assert call["repo_path"] == str(tmp_path)
    assert hasattr(call["plan_input"], "architecture_overview")
    assert call["plan_input"].architecture_overview == "Backend FastAPI; frontend Angular."
    mock_agents["tech_lead"].run.assert_not_called()
    mock_agents["architecture"].run.assert_not_called()
