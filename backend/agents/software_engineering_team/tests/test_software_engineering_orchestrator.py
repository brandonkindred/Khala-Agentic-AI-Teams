"""Unit tests for the orchestrator.

Routed through the in-memory ``FakeJobServiceClient`` via the autouse
``_autouse_patched_job_store`` fixture, so direct ``job_store`` calls in
these tests no longer require a live job service.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import orchestrator
import pytest

from llm_service import OLLAMA_WEEKLY_LIMIT_MESSAGE, LLMRateLimitError
from shared_command_runner.runner import CommandResult
from software_engineering_team.shared.models import (
    ProductRequirements,
    SystemArchitecture,
    Task,
    TaskAssignment,
    TaskType,
)


@pytest.fixture(autouse=True)
def _autouse_patched_job_store(patched_job_store):
    return patched_job_store


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
        '    response = client.get("/test-generic-error")'
    )
    mock_result = CommandResult(
        success=False,
        exit_code=1,
        stdout=failure_summary,
        stderr="",
    )

    with patch(
        "shared_command_runner.runner.run_python_syntax_check",
        return_value=CommandResult(True, 0, "", ""),
    ):
        with patch("shared_command_runner.runner.run_pytest", return_value=mock_result):
            ok, error_output = orchestrator._run_build_verification(tmp_path, "backend", "task-1")

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
        (jid, kw) for jid, kw in update_job_calls if kw.get("status") == "paused_llm_limit"
    ]
    assert len(paused_calls) >= 1
    assert paused_calls[0][1]["error"] == OLLAMA_WEEKLY_LIMIT_MESSAGE


def _seed_retry_job(tmp_path: Path, job_id: str) -> None:
    """Seed a job record as a prior coding-team run leaves it: a task_graph_snapshot plus the
    requirements/architecture fields ``run_failed_tasks`` reads to rebuild its plan input."""
    from software_engineering_team.shared.job_store import create_job, update_job

    create_job(job_id, str(tmp_path))
    update_job(
        job_id,
        task_graph_snapshot=[{"id": "t1", "status": "failed", "title": "Backend task"}],
        failed_tasks=[{"task_id": "t1", "reason": "prior fail", "title": "Backend task"}],
        requirements_title="Task Manager API",
        architecture_overview="API + frontend",
        resolved_questions=[{"question": "q?", "answer": "a"}],
    )


def test_run_failed_tasks_delegates_to_coding_team(tmp_path: Path) -> None:
    """The retry path rebuilds a plan input from the stored record and delegates to
    run_coding_team_orchestrator with retry_failed=True, then emits coding-team metrics."""
    job_id = "test-retry-delegates"
    _seed_retry_job(tmp_path, job_id)

    captured = {}

    def fake_delegate(jid, repo_path, plan_input, **kwargs):
        captured["job_id"] = jid
        captured["repo_path"] = repo_path
        captured["plan_input"] = plan_input
        captured["kwargs"] = kwargs

    emit_called = MagicMock()

    with patch("coding_team.orchestrator.run_coding_team_orchestrator", side_effect=fake_delegate):
        with patch("orchestrator._emit_coding_team_metrics", emit_called):
            orchestrator.run_failed_tasks(job_id)

    assert captured["job_id"] == job_id
    assert captured["repo_path"] == str(tmp_path.resolve())
    assert captured["kwargs"]["retry_failed"] is True
    plan_input = captured["plan_input"]
    assert plan_input.repo_path == str(tmp_path.resolve())
    assert plan_input.requirements_title == "Task Manager API"
    assert plan_input.architecture_overview == "API + frontend"
    assert plan_input.resolved_questions == [{"question": "q?", "answer": "a"}]
    emit_called.assert_called_once_with(job_id)

    # The pre-retry failed_tasks list is cleared at the RUNNING transition so the status endpoint
    # and retry gate do not keep reporting stale failures (the coding-team run never writes it).
    from software_engineering_team.shared.job_store import get_job

    assert get_job(job_id).get("failed_tasks") == []


def test_run_failed_tasks_marks_failed_on_delegate_error(tmp_path: Path) -> None:
    """An unexpected error from the coding-team run is mapped to a terminal failed status."""
    job_id = "test-retry-delegate-error"
    _seed_retry_job(tmp_path, job_id)

    update_job_calls = []

    def capture_update_job(jid, cache_dir=None, **kwargs):
        update_job_calls.append((jid, kwargs))

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch(
            "coding_team.orchestrator.run_coding_team_orchestrator",
            side_effect=RuntimeError("boom"),
        ):
            orchestrator.run_failed_tasks(job_id)

    failed_calls = [
        kw for _jid, kw in update_job_calls if kw.get("status") == orchestrator.JOB_STATUS_FAILED
    ]
    assert failed_calls
    assert failed_calls[-1]["error"] == "boom"


def test_run_failed_tasks_cancelled_on_cancellation(tmp_path: Path) -> None:
    """A CancellationError from the coding-team run yields a terminal cancelled status."""
    job_id = "test-retry-cancelled"
    _seed_retry_job(tmp_path, job_id)

    update_job_calls = []

    def capture_update_job(jid, cache_dir=None, **kwargs):
        update_job_calls.append((jid, kwargs))

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch(
            "coding_team.orchestrator.run_coding_team_orchestrator",
            side_effect=orchestrator.CancellationError("cancelled"),
        ):
            orchestrator.run_failed_tasks(job_id)

    cancelled_calls = [
        kw for _jid, kw in update_job_calls if kw.get("status") == orchestrator.JOB_STATUS_CANCELLED
    ]
    assert cancelled_calls


def test_run_failed_tasks_raises_when_job_missing() -> None:
    """A retry for an unknown job raises rather than silently no-op'ing."""
    with pytest.raises(ValueError, match="not found"):
        orchestrator.run_failed_tasks("no-such-job")


def test_run_failed_tasks_raises_without_repo_path(tmp_path: Path) -> None:
    """A job record with no repo_path cannot be resumed."""
    from software_engineering_team.shared.job_store import create_job, get_job, update_job

    job_id = "test-retry-no-repo"
    create_job(job_id, str(tmp_path))
    # Clear repo_path to simulate a malformed record.
    update_job(job_id, repo_path=None, task_graph_snapshot=[{"id": "t1", "status": "failed"}])
    assert get_job(job_id) is not None
    with pytest.raises(ValueError, match="no repo_path"):
        orchestrator.run_failed_tasks(job_id)


def test_run_failed_tasks_raises_without_snapshot(tmp_path: Path) -> None:
    """A job that never ran the coding team has no task graph snapshot to resume."""
    from software_engineering_team.shared.job_store import create_job

    job_id = "test-retry-no-snapshot"
    create_job(job_id, str(tmp_path))
    with pytest.raises(ValueError, match="no task graph snapshot"):
        orchestrator.run_failed_tasks(job_id)


def test_run_orchestrator_fails_job_when_planning_raises_no_fallback(tmp_path: Path) -> None:
    """When Planning workflow fails (success=False), job fails with planning error."""
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
                with patch(
                    "product_requirements_analysis_agent.ProductRequirementsAnalysisAgent",
                    return_value=mock_pra_agent,
                ):
                    with patch("planning_team.orchestrator.run_workflow") as mock_run_planning:
                        mock_run_planning.return_value = {
                            "success": False,
                            "failure_reason": "Planning failed",
                        }
                        orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [(jid, kw) for jid, kw in update_job_calls if kw.get("status") == "failed"]
    assert len(failed_calls) >= 1
    assert "planning" in failed_calls[0][1].get("error", "").lower()
    assert len(arch_inputs_received) == 0


def test_run_orchestrator_fails_job_when_project_planning_raises(tmp_path: Path) -> None:
    """When Planning workflow fails (success=False), job is marked failed."""
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
                with patch(
                    "product_requirements_analysis_agent.ProductRequirementsAnalysisAgent",
                    return_value=mock_pra_agent,
                ):
                    with patch("planning_team.orchestrator.run_workflow") as mock_run_planning:
                        mock_run_planning.return_value = {
                            "success": False,
                            "failure_reason": "Planning failed",
                        }
                        orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [(jid, kw) for jid, kw in update_job_calls if kw.get("status") == "failed"]
    assert len(failed_calls) >= 1
    assert "planning" in failed_calls[0][1].get("error", "").lower()


def test_run_orchestrator_invokes_coding_team_not_legacy_tech_lead_or_v2_workers(
    tmp_path: Path,
) -> None:
    """Main path: after Planning and adapter, run_coding_team_orchestrator is called; Tech Lead and v2 workers are not."""
    from planning_adapter import PlanningAdapterResult

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

    adapter_result = PlanningAdapterResult(
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
        coding_team_calls.append(
            {"job_id": jid, "repo_path": repo_path, "plan_input": plan_input, **kwargs}
        )
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
                    with patch("planning_team.orchestrator.run_workflow") as mock_run_planning:
                        mock_run_planning.return_value = {
                            "success": True,
                            "handoff_package": {
                                "architecture_overview": "Backend FastAPI; frontend Angular."
                            },
                            "failure_reason": None,
                        }
                        with patch(
                            "planning_adapter.adapt_planning_result",
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
