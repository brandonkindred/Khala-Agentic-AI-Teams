"""Tests for the Backend/Frontend Code V2 documentation and deliver phases.

These phases are identical between backend and frontend code-v2 teams, but
import paths differ. Tests target both.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


def _task(**overrides):
    from software_engineering_team.shared.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.FRONTEND,
        title="My task",
        description="desc",
        assignee="frontend",
    )
    base.update(overrides)
    return Task(**base)


# ===========================================================================
# Frontend Code V2 documentation phase
# ===========================================================================


def test_fe_documentation_phase_no_agent() -> None:
    from software_engineering_team.frontend_code_v2_team.models import (
        ExecutionResult,
        PlanningResult,
    )
    from software_engineering_team.frontend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )

    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=Path("/tmp"),
        execution_result=ExecutionResult(),
        planning_result=PlanningResult(),
        tool_agents={},
    )
    assert result.iterations == 0
    assert "no documentation agent" in result.summary.lower()


def test_fe_documentation_phase_missing_methods() -> None:
    from software_engineering_team.frontend_code_v2_team.models import (
        ExecutionResult,
        PlanningResult,
        ToolAgentKind,
    )
    from software_engineering_team.frontend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )

    # Agent without review/problem_solve
    bare = object()
    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=Path("/tmp"),
        execution_result=ExecutionResult(),
        planning_result=PlanningResult(),
        tool_agents={ToolAgentKind.DOCUMENTATION: bare},
    )
    assert "missing required" in result.summary


def test_fe_documentation_phase_clean_review(tmp_path: Path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import (
        ExecutionResult,
        PlanningResult,
        ToolAgentKind,
    )
    from software_engineering_team.frontend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )

    fake_doc = SimpleNamespace(
        review=lambda inp: SimpleNamespace(issues=[]),
        problem_solve=lambda inp: SimpleNamespace(files={}),
    )
    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=ExecutionResult(),
        planning_result=PlanningResult(),
        tool_agents={ToolAgentKind.DOCUMENTATION: fake_doc},
    )
    assert result.iterations == 1
    assert result.issues_fixed == 0


def test_fe_documentation_phase_fixes_issues(tmp_path: Path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import (
        ExecutionResult,
        PlanningResult,
        ToolAgentKind,
    )
    from software_engineering_team.frontend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )

    state = {"i": 0}

    def review(_):
        # First call: 1 issue, second: 0 issues
        if state["i"] == 0:
            state["i"] += 1
            from software_engineering_team.frontend_code_v2_team.models import ReviewIssue
            return SimpleNamespace(issues=[ReviewIssue(description="fix-readme")])
        return SimpleNamespace(issues=[])

    fake_doc = SimpleNamespace(
        review=review,
        problem_solve=lambda inp: SimpleNamespace(files={"README.md": "updated"}),
    )
    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=ExecutionResult(),
        planning_result=PlanningResult(),
        tool_agents={ToolAgentKind.DOCUMENTATION: fake_doc},
    )
    assert result.iterations == 2
    assert result.issues_fixed == 1
    assert (tmp_path / "README.md").read_text() == "updated"


def test_fe_documentation_phase_review_exception(tmp_path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import (
        ExecutionResult,
        PlanningResult,
        ToolAgentKind,
    )
    from software_engineering_team.frontend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )

    def boom(_):
        raise RuntimeError("review broke")

    fake_doc = SimpleNamespace(review=boom, problem_solve=lambda inp: None)
    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=ExecutionResult(),
        planning_result=PlanningResult(),
        tool_agents={ToolAgentKind.DOCUMENTATION: fake_doc},
    )
    assert result.iterations == 1


def test_fe_documentation_phase_problem_solve_exception(tmp_path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import (
        ExecutionResult,
        PlanningResult,
        ToolAgentKind,
    )
    from software_engineering_team.frontend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )

    def boom(_):
        raise RuntimeError("ps broke")

    from software_engineering_team.frontend_code_v2_team.models import ReviewIssue
    fake_doc = SimpleNamespace(
        review=lambda inp: SimpleNamespace(issues=[ReviewIssue(description="i1")]),
        problem_solve=boom,
    )
    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=ExecutionResult(),
        planning_result=PlanningResult(),
        tool_agents={ToolAgentKind.DOCUMENTATION: fake_doc},
    )
    assert result.iterations == 1


def test_fe_documentation_phase_problem_solve_no_files(tmp_path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import (
        ExecutionResult,
        PlanningResult,
        ReviewIssue,
        ToolAgentKind,
    )
    from software_engineering_team.frontend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )
    fake_doc = SimpleNamespace(
        review=lambda inp: SimpleNamespace(issues=[ReviewIssue(description="i1")]),
        problem_solve=lambda inp: SimpleNamespace(files={}),
    )
    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=tmp_path,
        execution_result=ExecutionResult(),
        planning_result=PlanningResult(),
        tool_agents={ToolAgentKind.DOCUMENTATION: fake_doc},
    )
    assert result.iterations == 1
    assert result.issues_fixed == 0


# ===========================================================================
# Backend Code V2 documentation phase — identical to FE; cover symmetric path
# ===========================================================================


def test_be_documentation_phase_no_agent() -> None:
    from software_engineering_team.backend_code_v2_team.models import (
        ExecutionResult as _Exec,
    )
    from software_engineering_team.backend_code_v2_team.models import (
        PlanningResult as _Plan,
    )
    from software_engineering_team.backend_code_v2_team.phases.documentation import (
        run_documentation_phase,
    )

    result = run_documentation_phase(
        llm=MagicMock(),
        task=_task(),
        repo_path=Path("/tmp"),
        execution_result=_Exec(),
        planning_result=_Plan(),
        tool_agents={},
    )
    assert result.iterations == 0


# ===========================================================================
# Frontend Code V2 deliver phase
# ===========================================================================


def test_fe_deliver_no_files_returns_empty_summary(tmp_path: Path) -> None:
    from software_engineering_team.frontend_code_v2_team.phases.deliver import run_deliver

    result = run_deliver(task_id="t1", repo_path=tmp_path, files={}, summary="")
    assert result.merged is False
    assert "No files to deliver" in result.summary


def test_fe_deliver_git_agent_success(tmp_path: Path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.phases.deliver import run_deliver

    class _GitAgent:
        def deliver(self, inp):
            return SimpleNamespace(success=True, summary="merged", files={})

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="",
        task_title="title",
        tool_agents={ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent()},
        feature_branch_name="feature/x",
    )
    assert result.merged is True
    assert result.commit_messages


def test_fe_deliver_other_tool_agent_appends_files(tmp_path: Path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.phases.deliver import run_deliver

    class _DocsAgent:
        def deliver(self, inp):
            return SimpleNamespace(files={"docs.md": "hi"}, success=True, summary="")

    class _GitAgent:
        def deliver(self, inp):
            # ensure docs.md got passed through current_files
            assert "docs.md" in inp.current_files
            return SimpleNamespace(success=True, summary="merged", files={})

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="",
        tool_agents={
            ToolAgentKind.DOCUMENTATION: _DocsAgent(),
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent(),
        },
    )
    assert result.merged is True


def test_fe_deliver_tool_agent_exception_isolated(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.phases.deliver import run_deliver

    class _ExplodingDocs:
        def deliver(self, inp):
            raise RuntimeError("docs broke")

    class _GitAgent:
        def deliver(self, inp):
            return SimpleNamespace(success=True, summary="ok", files={})

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="",
        tool_agents={
            ToolAgentKind.DOCUMENTATION: _ExplodingDocs(),
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: _GitAgent(),
        },
    )
    assert result.merged is True


def test_fe_deliver_git_agent_failure_falls_through_to_inline(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.phases import deliver

    class _BadGit:
        def deliver(self, inp):
            raise RuntimeError("git fail")

    # Stub the inline git helpers so we don't touch a real repo
    monkeypatch.setattr(deliver, "create_feature_branch", lambda *a, **kw: (True, "feature/x"))
    monkeypatch.setattr(deliver, "write_agent_output", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(deliver, "merge_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(deliver, "delete_branch", lambda *a, **kw: True)
    monkeypatch.setattr(deliver, "checkout_branch", lambda *a, **kw: True)
    monkeypatch.setattr(deliver, "abort_merge", lambda *a, **kw: True)

    result = deliver.run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="ok",
        tool_agents={ToolAgentKind.GIT_BRANCH_MANAGEMENT: _BadGit()},
    )
    assert result.merged is True


def test_fe_deliver_inline_create_branch_fails(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.phases import deliver

    monkeypatch.setattr(deliver, "create_feature_branch", lambda *a, **kw: (False, "no perms"))
    monkeypatch.setattr(deliver, "checkout_branch", lambda *a, **kw: True)

    result = deliver.run_deliver(
        task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary=""
    )
    assert result.merged is False
    assert "Feature branch creation failed" in result.summary


def test_fe_deliver_inline_write_fails(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.phases import deliver

    monkeypatch.setattr(deliver, "create_feature_branch", lambda *a, **kw: (True, "feature/x"))
    monkeypatch.setattr(deliver, "write_agent_output", lambda *a, **kw: (False, "write err"))
    monkeypatch.setattr(deliver, "checkout_branch", lambda *a, **kw: True)

    result = deliver.run_deliver(
        task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary=""
    )
    assert result.merged is False
    assert "Write failed" in result.summary


def test_fe_deliver_inline_merge_fails(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.phases import deliver

    monkeypatch.setattr(deliver, "create_feature_branch", lambda *a, **kw: (True, "feature/x"))
    monkeypatch.setattr(deliver, "write_agent_output", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(deliver, "merge_branch", lambda *a, **kw: (False, "conflict"))
    monkeypatch.setattr(deliver, "abort_merge", lambda *a, **kw: True)
    monkeypatch.setattr(deliver, "checkout_branch", lambda *a, **kw: True)

    result = deliver.run_deliver(
        task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary=""
    )
    assert result.merged is False
    assert "Merge failed" in result.summary


def test_fe_deliver_inline_happy_path(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.phases import deliver

    monkeypatch.setattr(deliver, "create_feature_branch", lambda *a, **kw: (True, "feature/x"))
    monkeypatch.setattr(deliver, "write_agent_output", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(deliver, "merge_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(deliver, "delete_branch", lambda *a, **kw: True)
    monkeypatch.setattr(deliver, "checkout_branch", lambda *a, **kw: True)

    result = deliver.run_deliver(
        task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary="impl"
    )
    assert result.merged is True


def test_be_deliver_inline_happy_path(tmp_path: Path, monkeypatch) -> None:
    """Backend deliver is identical; exercise the inline branch."""
    from software_engineering_team.backend_code_v2_team.phases import deliver

    monkeypatch.setattr(deliver, "create_feature_branch", lambda *a, **kw: (True, "feature/x"))
    monkeypatch.setattr(deliver, "write_agent_output", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(deliver, "merge_branch", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(deliver, "delete_branch", lambda *a, **kw: True)
    monkeypatch.setattr(deliver, "checkout_branch", lambda *a, **kw: True)

    result = deliver.run_deliver(
        task_id="t1", repo_path=tmp_path, files={"a.py": "x"}, summary="impl"
    )
    assert result.merged is True
