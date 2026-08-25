"""Tests for git_branch_management and build_specialist tool agents (frontend + backend v2)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Frontend helpers
# ---------------------------------------------------------------------------


def _fe_microtask():
    from software_engineering_team.codegen_team.models import (
        Microtask,
        ToolAgentKind,
    )

    return Microtask(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)


def _fe_phase_input(**kwargs):
    from software_engineering_team.codegen_team.models import (
        Phase,
        ToolAgentPhaseInput,
    )

    base = dict(
        phase=Phase.PLANNING,
        repo_path="/tmp",
        current_files={},
        task_title="t",
        task_description="d",
        task_id="t1",
        language="typescript",
    )
    base.update(kwargs)
    return ToolAgentPhaseInput(**base)


def _fe_tool_input():
    from software_engineering_team.codegen_team.models import ToolAgentInput

    return ToolAgentInput(
        microtask=_fe_microtask(),
        task_title="t",
        task_description="d",
        spec_content="",
        repo_path="/tmp",
    )


def _fe_review_issue(**kwargs):
    from software_engineering_team.codegen_team.models import ReviewIssue

    base = dict(
        source="build_specialist",
        severity="critical",
        description="d",
        file_path="",
        recommendation="",
    )
    base.update(kwargs)
    return ReviewIssue(**base)


# ---------------------------------------------------------------------------
# Backend helpers
# ---------------------------------------------------------------------------


def _be_microtask():
    from software_engineering_team.codegen_team.models import (
        Microtask,
        ToolAgentKind,
    )

    return Microtask(id="mt-1", title="t", description="d", tool_agent=ToolAgentKind.GENERAL)


def _be_phase_input(**kwargs):
    from software_engineering_team.codegen_team.models import (
        Phase,
        ToolAgentPhaseInput,
    )

    base = dict(
        phase=Phase.PLANNING,
        repo_path="/tmp",
        current_files={},
        task_title="t",
        task_description="d",
        task_id="t1",
        language="python",
    )
    base.update(kwargs)
    return ToolAgentPhaseInput(**base)


def _be_tool_input():
    from software_engineering_team.codegen_team.models import ToolAgentInput

    return ToolAgentInput(
        microtask=_be_microtask(),
        repo_path="/tmp",
        existing_code="",
        language="python",
    )


def _be_review_issue(**kwargs):
    from software_engineering_team.codegen_team.models import ReviewIssue

    base = dict(
        source="build_specialist",
        severity="critical",
        description="d",
        file_path="",
        recommendation="",
    )
    base.update(kwargs)
    return ReviewIssue(**base)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    # Explicit initial branch so a global init.defaultBranch=development does not
    # make the later ``checkout -b development`` fail (branch already exists).
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True, check=True
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, capture_output=True, check=True
    )
    # Need an initial commit for branches
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
    # Create development branch
    subprocess.run(
        ["git", "checkout", "-b", "development"], cwd=tmp_path, capture_output=True, check=True
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Frontend git_branch_management
# ---------------------------------------------------------------------------


class TestFEGitBranchManagement:
    def _agent(self):
        from software_engineering_team.shared.tool_agent_git_branch import (
            GitBranchManagementToolAgent,
        )

        return GitBranchManagementToolAgent()

    def test_execute(self):
        out = self._agent().execute(_fe_tool_input())
        assert "Git branch" in out.summary

    def test_run_delegates(self):
        out = self._agent().run(_fe_tool_input())
        assert "Git branch" in out.summary

    def test_plan(self):
        out = self._agent().plan(_fe_phase_input())
        assert out.recommendations

    def test_review(self):
        out = self._agent().review(_fe_phase_input())
        assert out.recommendations

    def test_problem_solve(self):
        out = self._agent().problem_solve(_fe_phase_input())
        assert out.recommendations

    def test_create_feature_branch_non_git(self, tmp_path: Path):
        ok, name = self._agent().create_feature_branch(tmp_path, "t1", "My Title")
        assert ok is False
        assert name is None

    def test_create_feature_branch_success(self, git_repo: Path):
        ok, name = self._agent().create_feature_branch(git_repo, "t1", "My Title")
        assert ok is True
        assert name and ("t1" in name)

    def test_commit_current_changes(self, git_repo: Path):
        (git_repo / "x.txt").write_text("y")
        ok, msg = self._agent().commit_current_changes(git_repo, "test commit")
        assert isinstance(ok, bool)
        assert isinstance(msg, str)

    def test_deliver_non_git(self, tmp_path: Path):
        out = self._agent().deliver(_fe_phase_input(repo_path=str(tmp_path)))
        assert out.success is False
        assert "Not a git repository" in out.summary

    def test_deliver_with_existing_branch(self, git_repo: Path):
        # Create feature branch first
        subprocess.run(
            ["git", "checkout", "-b", "feature/test"],
            cwd=git_repo,
            capture_output=True,
            check=False,
        )
        out = self._agent().deliver(
            _fe_phase_input(
                repo_path=str(git_repo),
                feature_branch_name="feature/test",
                task_id="t1",
                task_title="my title",
            )
        )
        # Just verify it produced a structured response
        assert hasattr(out, "success")
        assert isinstance(out.summary, str)

    def test_deliver_without_branch_creates_one(self, git_repo: Path, monkeypatch):
        # Stub write_agent_output to avoid actual filesystem work
        from software_engineering_team.shared import repo_writer

        monkeypatch.setattr(repo_writer, "write_agent_output", lambda *a, **kw: (True, ""))
        out = self._agent().deliver(
            _fe_phase_input(
                repo_path=str(git_repo),
                task_id="task1",
                task_title="My Title",
                current_files={"a.ts": "code"},
            )
        )
        assert hasattr(out, "success")


# ---------------------------------------------------------------------------
# Frontend build_specialist
# ---------------------------------------------------------------------------


class TestFEBuildSpecialist:
    def _agent(self):
        from software_engineering_team.codegen_team.tool_agents.frontend.build_specialist import (
            agent as mod,
        )

        a = mod.BuildSpecialistAdapterAgent.__new__(mod.BuildSpecialistAdapterAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute(self):
        a, _ = self._agent()
        out = a.execute(_fe_tool_input())
        assert "Build Specialist" in out.summary

    def test_run_delegates(self):
        a, _ = self._agent()
        out = a.run(_fe_tool_input())
        assert "Build Specialist" in out.summary

    def test_plan(self):
        a, _ = self._agent()
        out = a.plan(_fe_phase_input())
        assert out.recommendations

    def test_deliver(self):
        a, _ = self._agent()
        out = a.deliver(_fe_phase_input())
        assert "Build Specialist deliver" in out.summary

    def test_review_no_repo(self):
        a, _ = self._agent()
        out = a.review(_fe_phase_input(repo_path=""))
        assert "no repo_path" in out.summary

    def test_review_missing_repo_path(self, tmp_path: Path):
        a, _ = self._agent()
        out = a.review(_fe_phase_input(repo_path=str(tmp_path / "missing")))
        assert "repo path missing" in out.summary

    def test_review_no_package_json(self, tmp_path: Path):
        """If no package.json, returns 0 issues."""
        a, _ = self._agent()
        out = a.review(_fe_phase_input(repo_path=str(tmp_path)))
        # Project not detected -> empty issues
        assert out.issues == []

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        out = a.problem_solve(_fe_phase_input())
        assert "skipped" in out.summary

    def test_problem_solve_no_build_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(_fe_phase_input(review_issues=[_fe_review_issue(source="security")]))
        assert "No build issues" in out.summary

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()
        stub = MagicMock()
        stub.return_value = "## FILE a.ts ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n"
        monkeypatch.setattr(mod, "Agent", lambda *args, **kwargs: stub)
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.ts": "x"},
                review_issues=[
                    _fe_review_issue(source="build_specialist", file_path="a.ts"),
                    _fe_review_issue(source="build", file_path="b.ts"),
                    _fe_review_issue(source="tool_build_specialist", file_path="c.ts"),
                ],
            )
        )
        assert "3 of 3" in out.summary

    def test_problem_solve_llm_exception(self, monkeypatch):
        from llm_service.interface import LLMError

        a, mod = self._agent()
        a._model = object()

        class _StubAgent:
            def __call__(self, prompt):
                raise LLMError("err")

        monkeypatch.setattr(mod, "Agent", lambda *a, **kw: _StubAgent())
        out = a.problem_solve(
            _fe_phase_input(
                current_files={"a.ts": "x"},
                review_issues=[_fe_review_issue(source="build_specialist", file_path="a.ts")],
            )
        )
        assert "0 of 1" in out.summary


# ---------------------------------------------------------------------------
# Backend build_specialist
# ---------------------------------------------------------------------------


class TestBEBuildSpecialist:
    def _agent(self):
        from software_engineering_team.codegen_team.tool_agents.backend.build_specialist import (
            agent as mod,
        )

        a = mod.BuildSpecialistAdapterAgent.__new__(mod.BuildSpecialistAdapterAgent)
        a._model = None
        a.llm = None
        return a, mod

    def test_execute(self):
        a, _ = self._agent()
        out = a.execute(_be_tool_input())
        assert "Build Specialist" in out.summary

    def test_plan(self):
        a, _ = self._agent()
        out = a.plan(_be_phase_input())
        assert out.recommendations

    def test_deliver(self):
        a, _ = self._agent()
        out = a.deliver(_be_phase_input())
        assert "deliver" in out.summary.lower()

    def test_review_no_repo(self):
        a, _ = self._agent()
        out = a.review(_be_phase_input(repo_path=""))
        assert "no repo_path" in out.summary

    def test_review_missing_repo_path(self, tmp_path: Path):
        a, _ = self._agent()
        out = a.review(_be_phase_input(repo_path=str(tmp_path / "missing")))
        assert "repo path missing" in out.summary

    def test_review_no_python_files(self, tmp_path: Path):
        a, _ = self._agent()
        out = a.review(_be_phase_input(repo_path=str(tmp_path)))
        assert out.issues == []

    def test_problem_solve_no_model(self):
        a, _ = self._agent()
        out = a.problem_solve(_be_phase_input())
        assert "skipped" in out.summary

    def test_problem_solve_no_build_issues(self):
        a, _ = self._agent()
        a._model = object()
        out = a.problem_solve(_be_phase_input(review_issues=[_be_review_issue(source="security")]))
        assert "No build issues" in out.summary

    def test_problem_solve_fixes_issues(self, monkeypatch):
        a, mod = self._agent()
        a._model = object()

        class _Stub:
            def __call__(self, prompt):
                return "## FILE a.py ##\nfixed\n## SUMMARY ##\nok\n## END SUMMARY ##\n"

        monkeypatch.setattr(mod, "Agent", lambda *args, **kwargs: _Stub())
        out = a.problem_solve(
            _be_phase_input(
                current_files={"a.py": "x"},
                review_issues=[_be_review_issue(source="build_specialist", file_path="a.py")],
            )
        )
        assert "1 of 1" in out.summary
