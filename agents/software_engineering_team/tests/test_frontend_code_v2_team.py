"""
Unit tests for the frontend-code-v2 team: models, phases, tool agents, orchestrator.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import sys
_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from frontend_code_v2_team.models import (
    FrontendCodeV2WorkflowResult,
    DeliverResult,
    ExecutionResult,
    Microtask,
    MicrotaskStatus,
    Phase,
    PlanningResult,
    ProblemSolvingResult,
    ReviewIssue,
    ReviewResult,
    SetupResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)


class TestModels:
    def test_microtask_defaults(self):
        mt = Microtask(id="mt-1")
        assert mt.status == MicrotaskStatus.PENDING
        assert mt.tool_agent == ToolAgentKind.GENERAL
        assert mt.depends_on == []
        assert mt.output_files == {}

    def test_planning_result_defaults(self):
        pr = PlanningResult()
        assert pr.language == "typescript"
        assert pr.microtasks == []

    def test_workflow_result_defaults(self):
        wr = FrontendCodeV2WorkflowResult()
        assert not wr.success
        assert wr.current_phase == Phase.SETUP
        assert wr.iterations_used == 0
        assert wr.setup_result is None

    def test_phase_enum_includes_setup(self):
        assert Phase.SETUP.value == "setup"
        assert Phase.SETUP in Phase

    def test_tool_agent_kind_frontend_specific(self):
        assert ToolAgentKind.STATE_MANAGEMENT.value == "state_management"
        assert ToolAgentKind.UI_DESIGN.value == "ui_design"
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in ToolAgentKind
        assert ToolAgentKind.BUILD_SPECIALIST in ToolAgentKind

    def test_setup_result_model(self):
        sr = SetupResult(repo_initialized=True, readme_created=True, branch_created=True)
        assert sr.repo_initialized

    def test_tool_agent_io(self):
        mt = Microtask(id="mt-test", description="test")
        inp = ToolAgentInput(microtask=mt, repo_path="/tmp/repo", language="angular")
        assert inp.language == "angular"
        out = ToolAgentOutput(files={"app.component.ts": "content"}, summary="done")
        assert out.success


class TestSetupPhase:
    def test_run_setup_on_existing_repo(self, tmp_path):
        from frontend_code_v2_team.phases.setup import run_setup

        (tmp_path / ".git").mkdir()
        result = run_setup(repo_path=tmp_path, task_title="My App")
        assert isinstance(result, SetupResult)
        assert result.summary is not None

    def test_run_setup_creates_repo_when_missing(self, tmp_path):
        from frontend_code_v2_team.phases.setup import run_setup

        assert not (tmp_path / ".git").exists()
        result = run_setup(repo_path=tmp_path, task_title="New App")
        assert result.repo_initialized or (tmp_path / ".git").exists()
        assert result.summary


class TestPlanningPhase:
    def test_language_detection_angular(self, tmp_path):
        from frontend_code_v2_team.phases.planning import _detect_language
        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        (tmp_path / "angular.json").write_text("{}")
        task = Task(id="t1", type=TaskType.FRONTEND, assignee="frontend-code-v2", status=TaskStatus.PENDING, description="build ui")
        assert _detect_language(tmp_path, task) == "angular"

    def test_language_detection_from_description(self, tmp_path):
        from frontend_code_v2_team.phases.planning import _detect_language
        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        task = Task(id="t1", type=TaskType.FRONTEND, assignee="frontend-code-v2", status=TaskStatus.PENDING, description="Use React and TypeScript")
        assert _detect_language(tmp_path, task) == "react"

    def test_parse_planning_output(self):
        from frontend_code_v2_team.phases.planning import _parse_planning_output

        raw = {
            "microtasks": [
                {"id": "mt-1", "title": "Add component", "tool_agent": "ui_design", "description": "create component"},
                {"id": "mt-2", "title": "Add tests", "tool_agent": "testing_qa", "description": "unit tests", "depends_on": ["mt-1"]},
            ],
            "language": "angular",
            "summary": "Plan created",
        }
        result = _parse_planning_output(raw, "typescript")
        assert len(result.microtasks) == 2
        assert result.microtasks[0].tool_agent == ToolAgentKind.UI_DESIGN
        assert result.microtasks[1].depends_on == ["mt-1"]
        assert result.language == "angular"

    def test_run_planning_fallback(self, tmp_path):
        from frontend_code_v2_team.phases.planning import run_planning
        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## MICROTASKS ##\n## END MICROTASKS ##\n"
            "## LANGUAGE ##\ntypescript\n## END LANGUAGE ##\n"
            "## SUMMARY ##\nempty\n## END SUMMARY ##"
        )
        task = Task(id="t1", type=TaskType.FRONTEND, assignee="frontend-code-v2", status=TaskStatus.PENDING, description="build something")
        result = run_planning(llm=mock_llm, task=task, repo_path=tmp_path)
        assert len(result.microtasks) == 1
        assert result.microtasks[0].id == "mt-implement-task"


class TestToolAgents:
    def test_build_tool_agents_includes_all_kinds(self):
        from frontend_code_v2_team.orchestrator import _build_tool_agents

        agents = _build_tool_agents(MagicMock())
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in agents
        assert ToolAgentKind.BUILD_SPECIALIST in agents
        assert ToolAgentKind.UI_DESIGN in agents
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "create_feature_branch")
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "commit_current_changes")
        assert hasattr(agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT], "deliver")

    def test_git_agent_create_feature_branch(self, tmp_path):
        import subprocess
        from frontend_code_v2_team.tool_agents.git_branch_management import GitBranchManagementToolAgent

        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True, check=True)
        (tmp_path / "f").write_text("x")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "branch", "-m", "development"], cwd=tmp_path, capture_output=True, check=True)
        agent = GitBranchManagementToolAgent()
        ok, name = agent.create_feature_branch(tmp_path, "task-1", "Login page")
        assert ok is True
        assert name

    def test_git_agent_commit_current_changes(self, tmp_path):
        from frontend_code_v2_team.tool_agents.git_branch_management import GitBranchManagementToolAgent

        (tmp_path / ".git").mkdir()
        agent = GitBranchManagementToolAgent()
        ok, msg = agent.commit_current_changes(tmp_path, "wip: test")
        assert isinstance(ok, bool)
        assert isinstance(msg, str)

    def test_build_specialist_stub(self):
        from frontend_code_v2_team.tool_agents.build_specialist import BuildSpecialistAdapterAgent

        agent = BuildSpecialistAdapterAgent()
        out = agent.execute(ToolAgentInput(microtask=Microtask(id="mt-1"), repo_path="/tmp"))
        assert out.summary
        assert hasattr(agent, "plan")
        assert hasattr(agent, "review")
        assert hasattr(agent, "problem_solve")
        assert hasattr(agent, "deliver")


class TestFrontendDevelopmentAgent:
    def test_build_tool_runners(self):
        from frontend_code_v2_team.orchestrator import FrontendDevelopmentAgent
        from frontend_code_v2_team.models import ToolAgentKind
        from frontend_code_v2_team.tool_agents.state_management import StateManagementToolAgent
        from frontend_code_v2_team.tool_agents.git_branch_management import GitBranchManagementToolAgent

        agent = FrontendDevelopmentAgent(MagicMock())
        tool_agents = {
            ToolAgentKind.STATE_MANAGEMENT: StateManagementToolAgent(),
            ToolAgentKind.GIT_BRANCH_MANAGEMENT: GitBranchManagementToolAgent(),
        }
        runners = agent._build_tool_runners(tool_agents)
        assert ToolAgentKind.STATE_MANAGEMENT in runners
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in runners
