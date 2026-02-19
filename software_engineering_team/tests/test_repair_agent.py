"""Unit tests for the Repair Expert agent and crash handling."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from repair_agent.agent import RepairExpertAgent
from repair_agent.models import RepairInput, RepairOutput


def test_repair_agent_suggests_import_fix_for_name_error() -> None:
    """Repair agent suggests an import fix for NameError traceback."""
    traceback_str = """Traceback (most recent call last):
  File "software_engineering_team/backend_agent/agent.py", line 407, in _plan_task
    x = compute_spec_content_chars(spec)
NameError: name 'compute_spec_content_chars' is not defined
"""
    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "suggested_fixes": [
            {
                "file_path": "backend_agent/agent.py",
                "line_start": 1,
                "line_end": 15,
                "replacement_content": "from shared.context_sizing import compute_existing_code_chars, compute_spec_content_chars\n",
            }
        ],
        "summary": "Added missing import for compute_spec_content_chars",
    }
    agent = RepairExpertAgent(llm_client=mock_llm)
    result = agent.run(RepairInput(
        traceback=traceback_str,
        exception_type="NameError",
        exception_message="name 'compute_spec_content_chars' is not defined",
        task_id="backend-task-1",
        agent_type="backend",
        agent_source_path=Path(__file__).resolve().parent.parent,
    ))
    assert result.suggested_fixes
    assert len(result.suggested_fixes) == 1
    fix = result.suggested_fixes[0]
    assert "compute_spec_content_chars" in fix.get("replacement_content", "")
    assert fix.get("file_path") == "backend_agent/agent.py"
    assert result.summary


def test_repair_agent_returns_empty_when_no_fix() -> None:
    """Repair agent returns empty suggested_fixes when it cannot determine a fix."""
    mock_llm = MagicMock()
    mock_llm.get_max_context_tokens.return_value = 16384
    mock_llm.complete_json.return_value = {
        "suggested_fixes": [],
        "summary": "Unable to determine fix: ambiguous error",
    }
    agent = RepairExpertAgent(llm_client=mock_llm)
    result = agent.run(RepairInput(
        traceback="Traceback...",
        exception_type="RuntimeError",
        exception_message="Something went wrong",
        task_id="task-1",
        agent_type="backend",
        agent_source_path=Path(__file__).resolve().parent.parent,
    ))
    assert not result.suggested_fixes
    assert result.summary


def test_parse_traceback_for_crash_extracts_location() -> None:
    """_parse_traceback_for_crash extracts file_path, line_number, function_name."""
    import orchestrator
    try:
        raise NameError("test")
    except NameError as e:
        file_path, line_number, func_name = orchestrator._parse_traceback_for_crash(e)
    assert file_path
    assert "test_repair_agent" in str(file_path) or "orchestrator" in str(file_path)
    assert line_number is not None
    assert isinstance(line_number, int)


def test_log_agent_crash_banner_does_not_raise() -> None:
    """_log_agent_crash_banner logs without raising."""
    import orchestrator
    try:
        raise ValueError("test crash")
    except ValueError as e:
        orchestrator._log_agent_crash_banner("task-1", "backend", e, "")


def test_log_agent_crash_banner_logs_error_with_task_and_exception() -> None:
    """_log_agent_crash_banner logs at ERROR level with task_id and exception info."""
    import orchestrator
    error_calls = []
    original_error = orchestrator.logger.error

    def capture_error(msg, *args, **kwargs):
        error_calls.append((msg, args, kwargs))
        original_error(msg, *args, **kwargs)

    try:
        raise NameError("undefined_var")
    except NameError as e:
        with patch.object(orchestrator.logger, "error", side_effect=capture_error):
            orchestrator._log_agent_crash_banner("backend-task-1", "backend", e, "")
    assert len(error_calls) >= 3
    all_text = " ".join(str(c[0]) + " " + " ".join(str(a) for a in c[1]) for c in error_calls)
    assert "backend-task-1" in all_text
    assert "NameError" in all_text or "undefined_var" in all_text


def test_apply_repair_fixes_applies_valid_fix(tmp_path: Path) -> None:
    """_apply_repair_fixes applies a valid fix and returns True."""
    import orchestrator
    target_file = tmp_path / "test_file.py"
    target_file.write_text("line1\nline2\nline3\nline4\nline5\n")
    suggested_fixes = [
        {
            "file_path": str(target_file.name),
            "line_start": 2,
            "line_end": 2,
            "replacement_content": "fixed\n",
        }
    ]
    # agent_source_path is tmp_path so target is tmp_path/test_file.py
    applied = orchestrator._apply_repair_fixes(tmp_path, suggested_fixes)
    assert applied
    content = target_file.read_text()
    assert "fixed" in content
    assert "line2" not in content


def test_apply_repair_fixes_rejects_path_outside_tree(tmp_path: Path) -> None:
    """_apply_repair_fixes rejects paths outside agent_source_path."""
    import orchestrator
    agent_root = tmp_path / "software_engineering_team"
    agent_root.mkdir()
    (agent_root / "backend_agent").mkdir(parents=True)
    target = agent_root / "backend_agent" / "agent.py"
    target.write_text("x = 1\n")
    # Try to fix a path that resolves outside agent_root
    suggested_fixes = [
        {
            "file_path": "../../../etc/passwd",
            "line_start": 1,
            "line_end": 1,
            "replacement_content": "evil\n",
        }
    ]
    applied = orchestrator._apply_repair_fixes(agent_root, suggested_fixes)
    assert not applied


def test_orchestrator_repair_requeue_on_backend_crash(tmp_path: Path) -> None:
    """On backend NameError crash, repair applied, task re-queued, worker picks it up and completes."""
    import orchestrator
    from shared.models import Task, TaskType

    backend_dir = tmp_path / "backend"
    backend_dir.mkdir()
    (backend_dir / ".git").mkdir()
    frontend_dir = tmp_path / "frontend"
    frontend_dir.mkdir()

    task_id = "backend-task-1"
    task = Task(
        id=task_id,
        type=TaskType.BACKEND,
        title="Backend task",
        description="Implement API",
        assignee="backend",
    )
    backend_queue = [task_id]
    frontend_queue = []
    all_tasks = {task_id: task}
    completed = set()
    failed = {}
    completed_code_task_ids = []

    success_result = MagicMock()
    success_result.success = True

    mock_backend = MagicMock()
    mock_backend.run_workflow.side_effect = [
        NameError("name 'compute_spec_content_chars' is not defined"),
        success_result,
    ]

    mock_repair = MagicMock()
    mock_repair.run.return_value = RepairOutput(
        suggested_fixes=[{"file_path": "x.py", "line_start": 1, "line_end": 1, "replacement_content": "fix\n"}],
        summary="Fixed",
        applied=False,
    )

    mock_agents = {
        "backend": mock_backend,
        "frontend": MagicMock(),
        "git_setup": MagicMock(),
        "repair": mock_repair,
        "tech_lead": MagicMock(),
        "devops": MagicMock(),
        "qa": MagicMock(),
        "security": MagicMock(),
        "dbc_comments": MagicMock(),
        "code_review": MagicMock(),
        "accessibility": MagicMock(),
    }

    with patch("orchestrator.update_job"):
        with patch("shared.command_runner.ensure_backend_project_initialized") as mock_init:
            mock_init.return_value = MagicMock(success=True)
            with patch("orchestrator._apply_repair_fixes", return_value=True):
                orchestrator._run_backend_frontend_workers(
                    job_id="test-job",
                    path=tmp_path,
                    backend_dir=backend_dir,
                    frontend_dir=frontend_dir,
                    backend_queue=backend_queue,
                    frontend_queue=frontend_queue,
                    all_tasks=all_tasks,
                    completed=completed,
                    failed=failed,
                    completed_code_task_ids=completed_code_task_ids,
                    spec_content="# Spec",
                    architecture=MagicMock(),
                    agents=mock_agents,
                    tech_lead=MagicMock(),
                    total_tasks=1,
                    is_retry=False,
                )

    assert task_id in completed
    assert task_id not in failed
    assert mock_backend.run_workflow.call_count == 2
    mock_repair.run.assert_called_once()
