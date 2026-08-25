"""Tests for run_documentation_phase, proving parity between the backend_code_v2_team
and frontend_code_v2_team bindings of the shared documentation phase (both are thin
`functools.partial`-style bindings over
`software_engineering_team.shared.phases.documentation.run_documentation_phase_impl`)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


def _task(*, task_type, **overrides):
    from shared.dev_models.models import Task

    base = dict(
        id="t1",
        type=task_type,
        title="T",
        description="desc",
        assignee="backend",
    )
    base.update(overrides)
    return Task(**base)


def _backend_task(**overrides):
    from shared.dev_models.models import TaskType

    return _task(task_type=TaskType.BACKEND, **overrides)


def _frontend_task(**overrides):
    from shared.dev_models.models import TaskType

    return _task(task_type=TaskType.FRONTEND, assignee="frontend", **overrides)


def _execution_result(files, *, team="backend_code_v2_team"):
    if team == "backend_code_v2_team":
        from software_engineering_team.codegen_team.models import ExecutionResult
    else:
        from software_engineering_team.codegen_team.models import ExecutionResult

    return ExecutionResult(files=files)


def _planning_result(*, language="python", team="backend_code_v2_team"):
    if team == "backend_code_v2_team":
        from software_engineering_team.codegen_team.models import PlanningResult
    else:
        from software_engineering_team.codegen_team.models import PlanningResult

    return PlanningResult(language=language)


def _issue(*, file_path="x.py", team="backend_code_v2_team", **overrides):
    if team == "backend_code_v2_team":
        from software_engineering_team.codegen_team.models import ReviewIssue
    else:
        from software_engineering_team.codegen_team.models import ReviewIssue

    base = dict(
        source="documentation", severity="low", description="missing docstring", file_path=file_path
    )
    base.update(overrides)
    return ReviewIssue(**base)


def test_run_documentation_phase_no_agent(tmp_path: Path):
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_documentation_phase,
    )

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_backend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={},
    )
    assert "no documentation agent" in out.summary


def test_run_documentation_phase_missing_methods(tmp_path: Path):
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_documentation_phase,
    )

    bare = object()
    out = run_documentation_phase(
        llm=MagicMock(),
        task=_backend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: bare},
    )
    assert "missing required" in out.summary


def test_run_documentation_phase_clean_review(tmp_path: Path):
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[])

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_backend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.issues_fixed == 0
    assert out.iterations >= 1


def test_run_documentation_phase_review_raises(tmp_path: Path):
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.side_effect = RuntimeError("boom")
    doc_agent.problem_solve.return_value = MagicMock()  # ensure exists

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_backend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    # Loop breaks; we get partial result
    assert out is not None


def test_run_documentation_phase_problem_solve_raises(tmp_path: Path):
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[_issue()])
    doc_agent.problem_solve.side_effect = RuntimeError("oops")

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_backend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out is not None


def test_run_documentation_phase_writes_files(tmp_path: Path):
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.backend.profile import (
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
        task=_backend_task(),
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
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[_issue()])
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(files={})

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_backend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.py": "code"}),
        planning_result=_planning_result(),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.iterations == 1


def test_run_documentation_phase_max_iterations(tmp_path: Path):
    """If review keeps finding issues, stops at max_iterations."""
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.backend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[_issue()])
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(files={"x.py": "fixed"})

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_backend_task(),
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


# ---------------------------------------------------------------------------
# frontend_code_v2_team.phases._profile.run_documentation_phase
#
# Mirrors the backend_code_v2_team cases above one-for-one to prove the two
# teams' bound `run_documentation_phase` behave identically: both are bound
# via `shared.v2_phase_bindings.build_phase_bindings` over the same
# `shared.phases.documentation.run_documentation_phase_impl`, differing only
# by their `V2TeamConfig` (see `test_v2_team_config.py` for config parity).
# ---------------------------------------------------------------------------


def test_fe_run_documentation_phase_no_agent(tmp_path: Path):
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={},
    )
    assert "no documentation agent" in out.summary


def test_fe_run_documentation_phase_missing_methods(tmp_path: Path):
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    bare = object()
    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={ToolAgentKind.DOCUMENTATION: bare},
    )
    assert "missing required" in out.summary


def test_fe_run_documentation_phase_clean_review(tmp_path: Path):
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(issues=[])

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.issues_fixed == 0
    assert out.iterations >= 1


def test_fe_run_documentation_phase_review_raises(tmp_path: Path):
    from software_engineering_team.codegen_team.models import ToolAgentKind
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.side_effect = RuntimeError("boom")
    doc_agent.problem_solve.return_value = MagicMock()  # ensure exists

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out is not None


def test_fe_run_documentation_phase_problem_solve_raises(tmp_path: Path):
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(
        issues=[_issue(file_path="x.ts", team="frontend_code_v2_team")]
    )
    doc_agent.problem_solve.side_effect = RuntimeError("oops")

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out is not None


def test_fe_run_documentation_phase_writes_files(tmp_path: Path):
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.side_effect = [
        ToolAgentPhaseOutput(issues=[_issue(file_path="x.ts", team="frontend_code_v2_team")]),
        ToolAgentPhaseOutput(issues=[]),
    ]
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(
        files={"x.ts": "with tsdoc"},
    )

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.issues_fixed == 1
    assert (tmp_path / "x.ts").exists()
    assert (tmp_path / "x.ts").read_text() == "with tsdoc"


def test_fe_run_documentation_phase_problem_solve_no_files(tmp_path: Path):
    """If problem_solve returns no files, we stop iterating (frontend binding)."""
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(
        issues=[_issue(file_path="x.ts", team="frontend_code_v2_team")]
    )
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(files={})

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
    )
    assert out.iterations == 1


def test_fe_run_documentation_phase_max_iterations(tmp_path: Path):
    """If review keeps finding issues, stops at max_iterations (frontend binding)."""
    from software_engineering_team.codegen_team.models import (
        ToolAgentKind,
        ToolAgentPhaseOutput,
    )
    from software_engineering_team.codegen_team.stacks.frontend.profile import (
        run_documentation_phase,
    )

    doc_agent = MagicMock()
    doc_agent.review.return_value = ToolAgentPhaseOutput(
        issues=[_issue(file_path="x.ts", team="frontend_code_v2_team")]
    )
    doc_agent.problem_solve.return_value = ToolAgentPhaseOutput(files={"x.ts": "fixed"})

    out = run_documentation_phase(
        llm=MagicMock(),
        task=_frontend_task(),
        repo_path=tmp_path,
        execution_result=_execution_result({"x.ts": "code"}, team="frontend_code_v2_team"),
        planning_result=_planning_result(language="typescript", team="frontend_code_v2_team"),
        tool_agents={ToolAgentKind.DOCUMENTATION: doc_agent},
        max_iterations=2,
    )
    assert out.iterations == 2
