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
from shared.command_runner.executor import CommandResult
from shared.dev_models.models import (
    ProductRequirements,
    SystemArchitecture,
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
        "shared.command_runner.executor.run_python_syntax_check",
        return_value=CommandResult(True, 0, "", ""),
    ):
        with patch("shared.command_runner.executor.run_pytest", return_value=mock_result):
            from software_engineering_team import build_fix

            ok, error_output = build_fix._run_build_verification(tmp_path, "backend", "task-1")

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
            "software_engineering_team.spec_parser.parse_spec_with_llm",
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
        # Simulate a fully successful retry: the coding-team run merges the task and updates the
        # persisted snapshot via the injected callback.
        kwargs["update_job_fn"](task_graph_snapshot=[{"id": "t1", "status": "merged"}])

    emit_called = MagicMock()

    with patch(
        "software_engineering_team.coding_team_orchestrator.run_coding_team_orchestrator",
        side_effect=fake_delegate,
    ):
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

    # A fully successful retry leaves no FAILED tasks in the snapshot, so failed_tasks is cleared —
    # the status endpoint and retry gate no longer report the pre-retry failures.
    from software_engineering_team.shared.job_store import get_job

    assert get_job(job_id).get("failed_tasks") == []


def test_run_failed_tasks_repopulates_failed_from_snapshot(tmp_path: Path) -> None:
    """A retry that still leaves a FAILED task repopulates failed_tasks from the snapshot so the
    job stays visible/retryable (the coding-team run itself never writes failed_tasks)."""
    job_id = "test-retry-still-failing"
    _seed_retry_job(tmp_path, job_id)

    def fake_delegate(jid, repo_path, plan_input, **kwargs):
        kwargs["update_job_fn"](
            status="completed_with_failures",
            task_graph_snapshot=[
                {
                    "id": "t1",
                    "status": "failed",
                    "title": "Backend task",
                    "revision_feedback": [{"source": "engineer", "reason": "still broken"}],
                }
            ],
        )

    with patch(
        "software_engineering_team.coding_team_orchestrator.run_coding_team_orchestrator",
        side_effect=fake_delegate,
    ):
        with patch("orchestrator._emit_coding_team_metrics", MagicMock()):
            orchestrator.run_failed_tasks(job_id)

    from software_engineering_team.shared.job_store import get_job

    failed = get_job(job_id).get("failed_tasks")
    assert failed == [{"task_id": "t1", "title": "Backend task", "reason": "still broken"}]


def test_run_failed_tasks_marks_failed_on_delegate_error(tmp_path: Path) -> None:
    """An unexpected error from the coding-team run is mapped to a terminal failed status."""
    job_id = "test-retry-delegate-error"
    _seed_retry_job(tmp_path, job_id)

    update_job_calls = []

    def capture_update_job(jid, cache_dir=None, **kwargs):
        update_job_calls.append((jid, kwargs))

    with patch("orchestrator.update_job", side_effect=capture_update_job):
        with patch(
            "software_engineering_team.coding_team_orchestrator.run_coding_team_orchestrator",
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
            "software_engineering_team.coding_team_orchestrator.run_coding_team_orchestrator",
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


def _finalize_job(tmp_path: Path, job_id: str, snapshot: list) -> dict:
    """Seed a job with a snapshot, run the finalize reconciliation, and return the job record."""
    from software_engineering_team.shared.job_store import create_job, get_job, update_job

    create_job(job_id, str(tmp_path))
    update_job(job_id, task_graph_snapshot=snapshot, status="completed_with_failures")
    orchestrator._finalize_from_coding_snapshot(job_id)
    return get_job(job_id)


def test_finalize_repopulates_failed_tasks_from_snapshot(tmp_path: Path) -> None:
    """FAILED tasks in the snapshot become failed_tasks entries with task_id/title/reason."""
    data = _finalize_job(
        tmp_path,
        "fin-failed",
        [
            {"id": "t1", "status": "merged", "title": "OK"},
            {
                "id": "t2",
                "status": "failed",
                "title": "Broken",
                "revision_feedback": [
                    {"reason": "first attempt"},
                    {"reason": "gave up after revisions"},
                ],
            },
        ],
    )
    assert data.get("failed_tasks") == [
        {"task_id": "t2", "title": "Broken", "reason": "gave up after revisions"}
    ]
    # No LLM markers → the coding-team terminal status is left intact.
    assert data.get("status") == "completed_with_failures"


def test_finalize_clears_failed_tasks_on_clean_run(tmp_path: Path) -> None:
    """A snapshot with no FAILED tasks yields an empty failed_tasks list."""
    data = _finalize_job(tmp_path, "fin-clean", [{"id": "t1", "status": "merged", "title": "OK"}])
    assert data.get("failed_tasks") == []


def test_finalize_uses_build_and_review_feedback_shapes(tmp_path: Path) -> None:
    """Build-gate ({type,error}) and review ({description}) feedback still yield an actionable
    reason — not a blank string — even though neither uses the ``reason`` key."""
    data = _finalize_job(
        tmp_path,
        "fin-build",
        [
            {
                "id": "t1",
                "status": "failed",
                "title": "Build broke",
                "revision_feedback": [{"type": "build", "error": "ng build failed: TS2304"}],
            },
            {
                "id": "t2",
                "status": "failed",
                "title": "Review",
                "revision_feedback": [{"description": "missing input validation"}],
            },
        ],
    )
    reasons = {ft["task_id"]: ft["reason"] for ft in data.get("failed_tasks")}
    assert reasons == {"t1": "ng build failed: TS2304", "t2": "missing input validation"}
    assert data.get("status") == "completed_with_failures"


def test_finalize_ignores_stale_llm_marker_from_prior_attempt(tmp_path: Path) -> None:
    """A weekly-limit marker left in history by an earlier attempt must NOT re-pause a retry whose
    current (latest) failure is unrelated — only the latest reason per task drives the pause."""
    from llm_service import OLLAMA_WEEKLY_LIMIT_MESSAGE as WEEKLY

    data = _finalize_job(
        tmp_path,
        "fin-stale-marker",
        [
            {
                "id": "t1",
                "status": "failed",
                "title": "T1",
                "revision_feedback": [
                    {"reason": f"Implementation failed: {WEEKLY}"},  # stale, prior attempt
                    {"type": "build", "error": "unrelated build failure"},  # current failure
                ],
            }
        ],
    )
    assert data.get("status") == "completed_with_failures"  # not re-paused
    assert data.get("failed_tasks") == [
        {"task_id": "t1", "title": "T1", "reason": "unrelated build failure"}
    ]


def test_finalize_pauses_on_llm_weekly_limit(tmp_path: Path) -> None:
    """A failure reason carrying the Ollama weekly-limit marker overrides status to paused_llm_limit."""
    from llm_service import OLLAMA_WEEKLY_LIMIT_MESSAGE as WEEKLY

    data = _finalize_job(
        tmp_path,
        "fin-llm-limit",
        [
            {
                "id": "t1",
                "status": "failed",
                "title": "T1",
                "revision_feedback": [{"reason": f"Implementation failed: {WEEKLY}"}],
            }
        ],
    )
    assert data.get("status") == "paused_llm_limit"
    assert data.get("error") == WEEKLY
    assert data.get("failed_tasks")  # still recorded


def test_finalize_pauses_on_llm_connectivity(tmp_path: Path) -> None:
    """A connectivity marker overrides status to paused_llm_connectivity."""
    from software_engineering_team.shared.job_store import LLM_UNREACHABLE_AFTER_RETRIES

    data = _finalize_job(
        tmp_path,
        "fin-llm-conn",
        [
            {
                "id": "t1",
                "status": "failed",
                "title": "T1",
                "revision_feedback": [
                    {"reason": f"Implementation failed: {LLM_UNREACHABLE_AFTER_RETRIES}"}
                ],
            }
        ],
    )
    assert data.get("status") == orchestrator.JOB_STATUS_PAUSED_LLM_CONNECTIVITY
    assert data.get("error") == LLM_UNREACHABLE_AFTER_RETRIES


def test_finalize_noop_without_snapshot(tmp_path: Path) -> None:
    """No snapshot (or no job) → the helper does nothing and does not raise."""
    from software_engineering_team.shared.job_store import create_job, get_job

    create_job("fin-nosnap", str(tmp_path))
    orchestrator._finalize_from_coding_snapshot("fin-nosnap")
    assert "failed_tasks" not in get_job("fin-nosnap") or get_job("fin-nosnap").get(
        "failed_tasks"
    ) in (None, [])
    # Unknown job is a clean no-op.
    orchestrator._finalize_from_coding_snapshot("fin-does-not-exist")


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

    mock_agents = {
        "architecture": mock_arch,
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
                "software_engineering_team.spec_parser.parse_spec_with_llm",
                return_value=ProductRequirements(
                    title="Test App",
                    description="Build a todo app",
                    acceptance_criteria=[],
                    constraints=[],
                ),
            ):
                with patch(
                    "software_engineering_team.product_requirements_analysis_agent.ProductRequirementsAnalysisAgent",
                    return_value=mock_pra_agent,
                ):
                    with (
                        patch("planning_team.orchestrator.run_workflow") as mock_run_planning,
                        patch(
                            "software_engineering_team.shared.planning_audit.record_se_planning_run"
                        ) as mock_record_planning_run,
                    ):
                        mock_run_planning.return_value = {
                            "success": False,
                            "failure_reason": "Planning failed",
                        }
                        orchestrator.run_orchestrator(job_id, str(tmp_path))

    failed_calls = [(jid, kw) for jid, kw in update_job_calls if kw.get("status") == "failed"]
    assert len(failed_calls) >= 1
    assert "planning" in failed_calls[0][1].get("error", "").lower()
    assert len(arch_inputs_received) == 0
    mock_record_planning_run.assert_not_called()


# ``test_run_orchestrator_fails_job_when_project_planning_raises`` previously
# duplicated this planning-failure path with weaker assertions (no architecture-
# not-called check). Removed because the case is fully covered above: the second
# copy added no coverage and only duplicated setup.


def test_run_orchestrator_invokes_coding_team_not_v2_workers(
    tmp_path: Path,
) -> None:
    """Main path: after Planning and adapter, run_coding_team_orchestrator is called; the v2 workers are not."""
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
                "software_engineering_team.spec_parser.parse_spec_with_llm",
                return_value=ProductRequirements(
                    title="Test",
                    description="Desc",
                    acceptance_criteria=[],
                    constraints=[],
                ),
            ):
                with patch(
                    "software_engineering_team.product_requirements_analysis_agent.ProductRequirementsAnalysisAgent",
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
                            "software_engineering_team.planning_adapter.adapt_planning_result",
                            return_value=adapter_result,
                        ):
                            with (
                                patch(
                                    "software_engineering_team.coding_team_orchestrator.run_coding_team_orchestrator",
                                    side_effect=capture_run_coding_team,
                                ),
                                patch(
                                    "software_engineering_team.shared.planning_audit.record_se_planning_run"
                                ) as mock_record_planning_run,
                            ):
                                orchestrator.run_orchestrator(job_id, str(tmp_path))

    assert len(coding_team_calls) == 1
    call = coding_team_calls[0]
    assert call["job_id"] == job_id
    assert call["repo_path"] == str(tmp_path)
    assert hasattr(call["plan_input"], "architecture_overview")
    assert call["plan_input"].architecture_overview == "Backend FastAPI; frontend Angular."
    mock_agents["architecture"].run.assert_not_called()
    # The code-v2 team leads are not invoked either: the main path delegates
    # per-task work to the coding team (patched above). Asserted on
    # ``run_workflow`` — the v2 team-lead entry method — rather than ``.run`` so
    # the guard is meaningful (a MagicMock tracks attributes independently, so
    # ``.run.assert_not_called`` would pass even if ``run_workflow`` ran).
    mock_agents["frontend_code_v2"].run_workflow.assert_not_called()
    mock_agents["backend"].run_workflow.assert_not_called()
    mock_record_planning_run.assert_called_once_with(job_id, mock_run_planning.return_value)
