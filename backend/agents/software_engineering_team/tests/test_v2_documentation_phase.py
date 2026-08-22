"""Tests for backend_code_v2_team.phases._profile.run_documentation_phase."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.BACKEND,
        title="T",
        description="desc",
        assignee="backend",
    )
    base.update(overrides)
    return Task(**base)


def _execution_result(files):
    from software_engineering_team.backend_code_v2_team.models import ExecutionResult

    return ExecutionResult(files=files)


def _planning_result():
    from software_engineering_team.backend_code_v2_team.models import PlanningResult

    return PlanningResult(language="python")


def _issue(**overrides):
    from software_engineering_team.backend_code_v2_team.models import ReviewIssue

    base = dict(source="documentation", severity="low", description="missing docstring", file_path="x.py")
    base.update(overrides)
    return ReviewIssue(**base)


def test_run_documentation_phase_no_agent(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={},
    )
    assert "no documentation agent" in out.summary


def test_run_documentation_phase_missing_methods(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    bare = object()
    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: bare},
    )
    assert "missing required" in out.summary


def test_run_documentation_phase_clean_review(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[])

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.issues_fixed == 0
    assert out.iterations >= 1


def test_run_documentation_phase_review_raises(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.side_effect = RuntimeError("boom")
    doc_agent.problem_solve.return_value = MagicMock()  # ensure exists

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    # Loop breaks; we get partial result
    assert out is not None


def test_run_documentation_phase_problem_solve_raises(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[_issue()])
    doc_agent.problem_solve.side_effect = RuntimeError("oops")

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out is not None


def test_run_documentation_phase_writes_files(tmp_path: Path):
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    # First call: issues, second call: clean
    doc_agent.review.side_effect = [
        ToolAgentPhaseOutput(issues=[_issue()]),
        ToolAgentPhaseOutput(issues=[]),
    ]
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(
        files={"x.py": "with docstring"},
    )

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.issues_fixed == 1
    # File was written
    assert (tmp_path / "x.py").exists()
    assert (tmp_path / "x.py").read_text() == "with docstring"


def test_run_documentation_phase_problem_solve_no_files(tmp_path: Path):
    """If problem_solve returns no files, we stop iterating."""
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[_issue()])
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(files={})

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.iterations == 1


def test_run_documentation_phase_max_iterations(tmp_path: Path):
    """If review keeps finding issues, stops at max_iterations."""
    from software_engineering_team.backend_code_v2_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.backend_code_v2_team.phases._profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[_issue()])
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(files={"x.py": "fixed"})

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
        max_iterations=2,
    )
    assert out.iterations == 2


def test_write_files_creates_dirs(tmp_path: Path):
    from software_engineering_team.shared.phases.documentation import _write_files

    _write_files(tmp_path, {"docs/nested/x.md": "content"})
    assert (tmp_path / "docs" / "nested" / "x.md").exists()


def test_write_files_strips_leading_slash(tmp_path: Path):
    from software_engineering_team.shared.phases.documentation import _write_files

    _write_files(tmp_path, {"/x.md": "y"})
    assert (tmp_path / "x.md").exists()
