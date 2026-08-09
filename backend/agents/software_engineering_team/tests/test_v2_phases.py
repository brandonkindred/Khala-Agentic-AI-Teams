"""Tests for the Backend/Frontend Code V2 documentation and deliver phases.

These phases are identical between backend and frontend code-v2 teams, but
import paths differ. Tests target both.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, create_autospec


def _task(**overrides):
    from shared.dev_models.models import Task, TaskType

    base = dict(
        id="t1",
        type=TaskType.FRONTEND,
        title="My task",
        description="desc",
        assignee="frontend",
    )
    base.update(overrides)
    return Task(**base)


def _patch_autospec(monkeypatch, module, name: str, *, return_value=None, side_effect=None):
    mock = create_autospec(getattr(module, name), return_value=return_value)
    if side_effect is not None:
        mock.side_effect = side_effect
    monkeypatch.setattr(module, name, mock)
    return mock


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
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

    result = run_deliver(task_id="t1", repo_path=tmp_path, files={}, summary="")
    assert result.merged is False
    assert result.delivered_files == []
    assert "No files to deliver" in result.summary


def test_fe_deliver_git_agent_success(tmp_path: Path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

    class _GitAgent:
        def deliver(self, inp):
            assert inp.feature_branch_name == "feature/x"
            assert inp.task_id == "t1"
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
    assert result.delivered_files == ["a.ts"]
    assert result.commit_messages


def test_fe_deliver_other_tool_agent_appends_files(tmp_path: Path) -> None:
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

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
    assert result.delivered_files == ["a.ts", "docs.md"]


def test_fe_deliver_tool_agent_exception_isolated(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

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


def test_fe_deliver_tool_agents_empty_files_skip_git_agent_merge(
    tmp_path: Path, monkeypatch
) -> None:
    """Empty tool-agent delivery skips Git agent work in merge mode."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

    class _DocsAgent:
        def deliver(self, inp):
            return SimpleNamespace(files={}, success=True, summary="")

    class _GitAgent:
        def __init__(self) -> None:
            self.called = False

        def deliver(self, inp):
            self.called = True
            return SimpleNamespace(success=True, summary="merged", files={})

    git_agent = _GitAgent()
    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x")
    )

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={},
        summary="impl",
        tool_agents={
            ToolAgentKind.DOCUMENTATION: _DocsAgent(),
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: git_agent,
        },
    )

    assert result.merged is False
    assert result.delivered_files == []
    assert "No files to deliver" in result.summary
    assert git_agent.called is False
    create_mock.assert_not_called()


def test_fe_deliver_git_agent_failure_falls_through_to_inline(tmp_path: Path, monkeypatch) -> None:
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    class _BadGit:
        def deliver(self, inp):
            raise RuntimeError("git fail")

    # Stub the inline git helpers so we don't touch a real repo.
    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x"))
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "abort_merge", return_value=True)

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="ok",
        tool_agents={ToolAgentKind.GIT_BRANCH_MANAGEMENT: _BadGit()},
    )
    assert result.merged is True


def test_fe_deliver_inline_create_branch_fails(tmp_path: Path, monkeypatch) -> None:
    """Frontend inline delivery reports feature-branch creation failures."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(False, "no perms"))
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary="")
    assert result.merged is False
    assert "Feature branch creation failed" in result.summary


def test_fe_deliver_inline_write_fails(tmp_path: Path, monkeypatch) -> None:
    """Frontend inline delivery reports write failures after branch creation."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x"))
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(False, "write err"))
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary="")
    assert result.merged is False
    assert "Write failed" in result.summary


def test_fe_deliver_inline_merge_fails(tmp_path: Path, monkeypatch) -> None:
    """Frontend inline delivery aborts and reports merge failures."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x"))
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(False, "conflict"))
    _patch_autospec(monkeypatch, git_utils, "abort_merge", return_value=True)
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary="")
    assert result.merged is False
    assert "Merge failed" in result.summary


def test_fe_deliver_inline_quality_gate_blocks_merge(tmp_path: Path, monkeypatch) -> None:
    """Regression: the pre-merge quality gate must skip the merge when a build
    verifier fails -- before this gate existed, the merge always proceeded
    once the commit succeeded."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x"))
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    merge_mock = _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="",
        build_verifier=lambda repo_path, label, task_id: (False, "build broke"),
        build_verify_label="frontend",
    )

    assert result.merged is False
    assert result.summary == "Pre-merge quality gate failed: Build failed: build broke"
    merge_mock.assert_not_called()


def test_fe_deliver_dispatches_real_git_agent_with_quality_gate_fields(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression: run_deliver must construct the real (Pydantic)
    ToolAgentPhaseInput with the quality-gate fields and route them through the
    real GitBranchManagementToolAgent. The other gate tests either bypass the
    inline-fallback path entirely (no tool_agents supplied) or drive the Git
    agent with a bare SimpleNamespace, which skips Pydantic validation -- a
    renamed/removed field on the team model would stay green in both while
    silently dropping gate configuration in production. This test would fail
    if build_verifier/build_verify_label/linting_tool_agent/lint_agent_type
    were removed from frontend_code_v2_team.models.ToolAgentPhaseInput.
    """
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import tool_agent_git_branch as tab_mod
    from software_engineering_team.shared.tool_agent_git_branch import (
        GitBranchManagementToolAgent,
    )

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(tab_mod, "commit_working_tree", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tab_mod, "merge_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tab_mod, "delete_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tab_mod, "checkout_branch", lambda *a, **k: (True, ""))

    build_calls: list[tuple[str, str]] = []

    def _build_verifier(repo_path, label, task_id):
        build_calls.append((label, task_id))
        return True, ""

    lint_calls: list = []

    class _LintAgent:
        def run(self, inp):
            lint_calls.append(inp)
            return SimpleNamespace(execution_result=SimpleNamespace(success=True), passed=True)

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="ok",
        feature_branch_name="feature/x",
        tool_agents={ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent()},
        build_verifier=_build_verifier,
        build_verify_label="frontend",
        linting_tool_agent=_LintAgent(),
        lint_agent_type="frontend",
    )

    assert result.merged is True
    assert build_calls == [("frontend", "t1")]
    assert len(lint_calls) == 1
    assert lint_calls[0].agent_type == "frontend"
    assert lint_calls[0].task_id == "t1"


def test_fe_deliver_inline_happy_path(tmp_path: Path, monkeypatch) -> None:
    """Frontend inline delivery exercises branch creation, write, merge, and cleanup."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x")
    )
    write_mock = _patch_autospec(
        monkeypatch, repo_writer, "write_agent_output", return_value=(True, "")
    )
    merge_mock = _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(True, ""))
    delete_mock = _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)
    checkout_mock = _patch_autospec(
        monkeypatch, git_utils, "checkout_branch", return_value=(True, "")
    )

    result = run_deliver(
        task_id="t1", repo_path=tmp_path, files={"a.ts": "x"}, summary="impl"
    )
    assert result.merged is True
    create_mock.assert_called_once()
    write_mock.assert_called_once()
    merge_mock.assert_called_once_with(tmp_path, "feature/x", git_utils.DEVELOPMENT_BRANCH)
    delete_mock.assert_called_once_with(tmp_path, "feature/x")
    checkout_mock.assert_called_once_with(tmp_path, git_utils.DEVELOPMENT_BRANCH)


def test_fe_deliver_handoff_branch_does_not_merge(tmp_path: Path, monkeypatch) -> None:
    """merge_to_development=False prepares a branch for external Tech Lead review."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x"))
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    commit_mock = _patch_autospec(
        monkeypatch, git_utils, "commit_working_tree", return_value=(True, "")
    )
    merge_mock = _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(True, ""))
    delete_mock = _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="impl",
        merge_to_development=False,
    )

    assert result.branch_ready is True
    assert result.merged is False
    assert result.branch_name == "feature/x"
    assert result.delivered_files == ["a.ts"]
    assert result.commit_messages
    assert commit_mock.call_args.args[1] == result.commit_messages[0]
    merge_mock.assert_not_called()
    delete_mock.assert_not_called()


def test_fe_deliver_sanitizes_task_id_for_branch_names(tmp_path: Path, monkeypatch) -> None:
    """Task IDs with invalid git characters are slugified before branch creation."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/safe")
    )
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "commit_working_tree", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(
        task_id="Task 1/Bad:ID",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="impl",
        task_title="Build UI",
        merge_to_development=False,
    )

    assert result.branch_ready is True
    suffix = create_mock.call_args.args[2]
    # Suffix carries a stable task-id hash to avoid cross-task branch collisions.
    assert suffix.startswith("task-1-bad-id-build-ui-")
    assert len(suffix.rsplit("-", 1)[-1]) == 8


def test_fe_deliver_handoff_with_tool_agent_appends_files(tmp_path: Path, monkeypatch) -> None:
    """Tool-agent output is included when handoff mode bypasses the Git agent."""
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    class _DocsAgent:
        def __init__(self) -> None:
            self.called = False

        def deliver(self, inp):
            self.called = True
            return SimpleNamespace(files={"docs.md": "hi"}, success=True, summary="")

    class _GitAgent:
        def __init__(self) -> None:
            self.called = False

        def deliver(self, inp):
            self.called = True
            return SimpleNamespace(success=True, summary="merged", files={})

    docs_agent = _DocsAgent()
    git_agent = _GitAgent()
    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x"))

    def _write(repo_path, payload, subdir=""):
        assert "docs.md" in payload.files
        return True, ""

    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", side_effect=_write)
    _patch_autospec(monkeypatch, git_utils, "commit_working_tree", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="impl",
        tool_agents={
            ToolAgentKind.DOCUMENTATION: docs_agent,
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: git_agent,
        },
        merge_to_development=False,
    )

    assert result.branch_ready is True
    assert result.delivered_files == ["a.ts", "docs.md"]
    assert docs_agent.called is True
    assert git_agent.called is False


def test_fe_deliver_handoff_with_tool_agents_no_files_skips_branch(
    tmp_path: Path, monkeypatch
) -> None:
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

    class _DocsAgent:
        def deliver(self, inp):
            return SimpleNamespace(files={}, success=True, summary="")

    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x")
    )

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={},
        summary="impl",
        tool_agents={ToolAgentKind.DOCUMENTATION: _DocsAgent()},
        merge_to_development=False,
    )

    assert result.branch_ready is False
    assert result.delivered_files == []
    assert "No files to deliver" in result.summary
    create_mock.assert_not_called()


def test_fe_deliver_handoff_create_branch_fails(tmp_path: Path, monkeypatch) -> None:
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver

    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(False, "no perms"))
    checkout_mock = _patch_autospec(
        monkeypatch, git_utils, "checkout_branch", return_value=(True, "")
    )

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="impl",
        merge_to_development=False,
    )

    assert result.branch_ready is False
    assert "Feature branch creation failed" in result.summary
    checkout_mock.assert_called_once()


def test_fe_deliver_handoff_commit_fails_cleans_created_branch(tmp_path: Path, monkeypatch) -> None:
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _patch_autospec(monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x"))
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "commit_working_tree", return_value=(False, "commit err"))
    checkout_mock = _patch_autospec(
        monkeypatch, git_utils, "checkout_branch", return_value=(True, "")
    )
    delete_mock = _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.ts": "x"},
        summary="impl",
        merge_to_development=False,
    )

    assert result.branch_ready is False
    assert "Commit failed" in result.summary
    checkout_mock.assert_called_once_with(tmp_path, git_utils.DEVELOPMENT_BRANCH)
    delete_mock.assert_called_once_with(tmp_path, "feature/x")


def _assert_restores_development_on_existing_branch_checkout_failure(
    run_deliver, git_utils, repo_writer, tmp_path, monkeypatch, caplog
) -> None:
    """Shared assertion for both v2 deliver phases: a failed checkout of a supplied
    feature branch reports the error AND restores the development branch."""
    seen: list[str] = []

    def _checkout(repo_path, branch):
        seen.append(branch)
        if branch == "feature/existing":
            return False, "checkout boom"
        return False, "restore boom"  # restore also fails → must be logged

    _patch_autospec(monkeypatch, git_utils, "checkout_branch", side_effect=_checkout)
    write_mock = _patch_autospec(
        monkeypatch, repo_writer, "write_agent_output", return_value=(True, "")
    )

    with caplog.at_level(logging.ERROR):
        result = run_deliver(
            task_id="t1",
            repo_path=tmp_path,
            files={"a.py": "x"},
            summary="impl",
            feature_branch_name="feature/existing",
            merge_to_development=False,
        )

    assert result.branch_ready is False
    assert "Feature branch checkout failed" in result.summary
    # Feature branch checked out first, development restored after the failure.
    assert seen == ["feature/existing", git_utils.DEVELOPMENT_BRANCH]
    # No files written when the branch could not be checked out.
    write_mock.assert_not_called()
    # The failed restoration is surfaced, not swallowed.
    assert "failed to restore" in caplog.text


def test_be_deliver_handoff_restores_development_on_checkout_failure(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    from shared.git import git_utils
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _assert_restores_development_on_existing_branch_checkout_failure(
        run_deliver, git_utils, repo_writer, tmp_path, monkeypatch, caplog
    )


def test_fe_deliver_handoff_restores_development_on_checkout_failure(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    from shared.git import git_utils
    from software_engineering_team.frontend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _assert_restores_development_on_existing_branch_checkout_failure(
        run_deliver, git_utils, repo_writer, tmp_path, monkeypatch, caplog
    )


def test_be_deliver_inline_happy_path(tmp_path: Path, monkeypatch) -> None:
    """Backend inline delivery exercises branch creation, write, merge, and cleanup."""
    from shared.git import git_utils
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/x")
    )
    write_mock = _patch_autospec(
        monkeypatch, repo_writer, "write_agent_output", return_value=(True, "")
    )
    merge_mock = _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(True, ""))
    delete_mock = _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)
    checkout_mock = _patch_autospec(
        monkeypatch, git_utils, "checkout_branch", return_value=(True, "")
    )

    result = run_deliver(
        task_id="t1", repo_path=tmp_path, files={"a.py": "x"}, summary="impl"
    )
    assert result.merged is True
    create_mock.assert_called_once()
    write_mock.assert_called_once()
    merge_mock.assert_called_once_with(tmp_path, "feature/x", git_utils.DEVELOPMENT_BRANCH)
    delete_mock.assert_called_once_with(tmp_path, "feature/x")
    checkout_mock.assert_called_once_with(tmp_path, git_utils.DEVELOPMENT_BRANCH)


def test_be_deliver_dispatches_real_git_agent_with_quality_gate_fields(
    tmp_path: Path, monkeypatch
) -> None:
    """Backend counterpart of the frontend real-model regression test above.

    ``backend_code_v2_team.models.ToolAgentPhaseInput`` is a separate Pydantic
    declaration from the frontend one -- a renamed/removed quality-gate field
    on just the backend model would stay green in every other test in this
    suite (frontend real-model test, shared SimpleNamespace-based Git-agent
    tests, forwarding tests that replace ``run_deliver`` entirely), so it
    needs its own direct coverage.
    """
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import tool_agent_git_branch as tab_mod
    from software_engineering_team.shared.tool_agent_git_branch import (
        GitBranchManagementToolAgent,
    )

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(tab_mod, "commit_working_tree", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tab_mod, "merge_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tab_mod, "delete_branch", lambda *a, **k: (True, ""))
    monkeypatch.setattr(tab_mod, "checkout_branch", lambda *a, **k: (True, ""))

    build_calls: list[tuple[str, str]] = []

    def _build_verifier(repo_path, label, task_id):
        build_calls.append((label, task_id))
        return True, ""

    lint_calls: list = []

    class _LintAgent:
        def run(self, inp):
            lint_calls.append(inp)
            return SimpleNamespace(execution_result=SimpleNamespace(success=True), passed=True)

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.py": "x"},
        summary="ok",
        feature_branch_name="feature/x",
        tool_agents={ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent()},
        build_verifier=_build_verifier,
        build_verify_label="backend",
        linting_tool_agent=_LintAgent(),
        lint_agent_type="backend",
    )

    assert result.merged is True
    assert build_calls == [("backend", "t1")]
    assert len(lint_calls) == 1
    assert lint_calls[0].agent_type == "backend"
    assert lint_calls[0].task_id == "t1"


def test_be_deliver_sanitizes_task_id_for_branch_names(tmp_path: Path, monkeypatch) -> None:
    """Backend delivery uses the same branch-safe task-id slug as frontend."""
    from shared.git import git_utils
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/api")
    )
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(True, ""))
    _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(
        task_id="API Task/Bad:ID",
        repo_path=tmp_path,
        files={"a.py": "x"},
        summary="impl",
        task_title="Build API",
    )

    assert result.merged is True
    suffix = create_mock.call_args.args[2]
    # Suffix carries a stable task-id hash to avoid cross-task branch collisions.
    assert suffix.startswith("api-task-bad-id-build-api-")
    assert len(suffix.rsplit("-", 1)[-1]) == 8


def test_be_deliver_handoff_branch_does_not_merge(tmp_path: Path, monkeypatch) -> None:
    """Backend deliver supports the same branch handoff mode."""
    from shared.git import git_utils
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/api")
    )
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(True, ""))
    commit_mock = _patch_autospec(
        monkeypatch, git_utils, "commit_working_tree", return_value=(True, "")
    )
    merge_mock = _patch_autospec(monkeypatch, git_utils, "merge_branch", return_value=(True, ""))
    delete_mock = _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)
    _patch_autospec(monkeypatch, git_utils, "checkout_branch", return_value=(True, ""))

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.py": "x"},
        summary="impl",
        merge_to_development=False,
    )

    assert result.branch_ready is True
    assert result.merged is False
    assert result.branch_name == "feature/api"
    assert result.delivered_files == ["a.py"]
    assert result.commit_messages
    assert commit_mock.call_args.args[1] == result.commit_messages[0]
    merge_mock.assert_not_called()
    delete_mock.assert_not_called()


def test_be_deliver_handoff_with_tool_agents_no_files_skips_branch(
    tmp_path: Path, monkeypatch
) -> None:
    from shared.git import git_utils
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver

    class _DocsAgent:
        def deliver(self, inp):
            return SimpleNamespace(files={}, success=True, summary="")

    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/api")
    )

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={},
        summary="impl",
        tool_agents={ToolAgentKind.DOCUMENTATION: _DocsAgent()},
        merge_to_development=False,
    )

    assert result.branch_ready is False
    assert result.delivered_files == []
    assert "No files to deliver" in result.summary
    create_mock.assert_not_called()


def test_be_deliver_tool_agents_empty_files_skip_git_agent_merge(
    tmp_path: Path, monkeypatch
) -> None:
    """Backend merge mode does not call the Git agent when tool agents produce no files."""
    from shared.git import git_utils
    from software_engineering_team.backend_code_v2_team.models import ToolAgentKind
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver

    class _DocsAgent:
        def deliver(self, inp):
            return SimpleNamespace(files={}, success=True, summary="")

    class _GitAgent:
        def __init__(self) -> None:
            self.called = False

        def deliver(self, inp):
            self.called = True
            return SimpleNamespace(success=True, summary="merged", files={})

    git_agent = _GitAgent()
    create_mock = _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/api")
    )

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={},
        summary="impl",
        tool_agents={
            ToolAgentKind.DOCUMENTATION: _DocsAgent(),
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: git_agent,
        },
    )

    assert result.merged is False
    assert result.delivered_files == []
    assert "No files to deliver" in result.summary
    assert git_agent.called is False
    create_mock.assert_not_called()


def test_be_deliver_handoff_write_fails_cleans_created_branch(tmp_path: Path, monkeypatch) -> None:
    """A failed handoff write restores development and deletes the fresh branch."""
    from shared.git import git_utils
    from software_engineering_team.backend_code_v2_team.orchestrator import run_deliver
    from software_engineering_team.shared import repo_writer

    _patch_autospec(
        monkeypatch, git_utils, "create_feature_branch", return_value=(True, "feature/api")
    )
    _patch_autospec(monkeypatch, repo_writer, "write_agent_output", return_value=(False, "write err"))
    checkout_mock = _patch_autospec(
        monkeypatch, git_utils, "checkout_branch", return_value=(True, "")
    )
    delete_mock = _patch_autospec(monkeypatch, git_utils, "delete_branch", return_value=True)

    result = run_deliver(
        task_id="t1",
        repo_path=tmp_path,
        files={"a.py": "x"},
        summary="impl",
        merge_to_development=False,
    )

    assert result.branch_ready is False
    assert "Write failed" in result.summary
    checkout_mock.assert_called_once_with(tmp_path, git_utils.DEVELOPMENT_BRANCH)
    delete_mock.assert_called_once_with(tmp_path, "feature/api")
