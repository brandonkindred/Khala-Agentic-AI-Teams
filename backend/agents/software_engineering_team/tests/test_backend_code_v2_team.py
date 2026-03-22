"""
Unit tests for the backend-code-v2 team: models, phases, tool agents, orchestrator.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

_team_dir = Path(__file__).resolve().parent.parent
if str(_team_dir) not in sys.path:
    sys.path.insert(0, str(_team_dir))

from backend_code_v2_team.models import (  # noqa: E402
    BackendCodeV2WorkflowResult,
    ExecutionResult,
    Microtask,
    MicrotaskStatus,
    Phase,
    PlanningResult,
    ReviewIssue,
    ReviewResult,
    SetupResult,
    ToolAgentInput,
    ToolAgentKind,
    ToolAgentOutput,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
)

# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class TestModels:
    def test_microtask_defaults(self):
        mt = Microtask(id="mt-1")
        assert mt.status == MicrotaskStatus.PENDING
        assert mt.tool_agent == ToolAgentKind.GENERAL
        assert mt.depends_on == []
        assert mt.output_files == {}

    def test_planning_result_defaults(self):
        pr = PlanningResult()
        assert pr.language == "python"
        assert pr.microtasks == []

    def test_workflow_result_defaults(self):
        wr = BackendCodeV2WorkflowResult()
        assert not wr.success
        assert wr.current_phase == Phase.SETUP
        assert wr.iterations_used == 0
        assert wr.setup_result is None

    def test_review_issue_model(self):
        issue = ReviewIssue(
            source="qa",
            severity="high",
            description="Missing error handler",
            file_path="app/main.py",
        )
        assert issue.severity == "high"

    def test_tool_agent_io(self):
        mt = Microtask(id="mt-test", description="test")
        inp = ToolAgentInput(microtask=mt, repo_path="/tmp/repo", language="java")
        assert inp.language == "java"
        out = ToolAgentOutput(files={"a.java": "class A {}"}, summary="done")
        assert out.success

    def test_phase_enum_includes_setup(self):
        assert Phase.SETUP.value == "setup"
        assert Phase.SETUP in Phase

    def test_setup_result_model(self):
        sr = SetupResult(repo_initialized=True, readme_created=True, branch_created=True)
        assert sr.repo_initialized
        assert sr.master_renamed_to_main is False

    def test_tool_agent_phase_input_output(self):
        inp = ToolAgentPhaseInput(phase=Phase.PLANNING, task_title="Build API", language="python")
        assert inp.phase == Phase.PLANNING
        out = ToolAgentPhaseOutput(recommendations=["Add auth"], success=True)
        assert out.success


# ---------------------------------------------------------------------------
# Setup phase tests
# ---------------------------------------------------------------------------

class TestSetupPhase:
    def test_run_setup_on_existing_repo(self, tmp_path):
        from backend_code_v2_team.phases.setup import run_setup

        (tmp_path / ".git").mkdir()
        result = run_setup(repo_path=tmp_path, task_title="My Project")
        assert isinstance(result, SetupResult)
        assert result.summary is not None

    def test_run_setup_creates_repo_when_missing(self, tmp_path):
        from backend_code_v2_team.phases.setup import run_setup

        assert not (tmp_path / ".git").exists()
        result = run_setup(repo_path=tmp_path, task_title="New Project")
        assert result.repo_initialized or (tmp_path / ".git").exists()
        assert result.summary


# ---------------------------------------------------------------------------
# Planning phase tests
# ---------------------------------------------------------------------------

class TestPlanningPhase:
    def test_language_detection_python(self, tmp_path):
        from backend_code_v2_team.phases.planning import _detect_language

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        (tmp_path / "requirements.txt").write_text("flask")
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build api")
        assert _detect_language(tmp_path, task) == "python"

    def test_language_detection_java(self, tmp_path):
        from backend_code_v2_team.phases.planning import _detect_language

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        (tmp_path / "pom.xml").write_text("<project/>")
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build api")
        assert _detect_language(tmp_path, task) == "java"

    def test_language_detection_from_description(self, tmp_path):
        from backend_code_v2_team.phases.planning import _detect_language

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="Use Spring Boot and Java")
        assert _detect_language(tmp_path, task) == "java"

    def test_parse_planning_output(self):
        from backend_code_v2_team.phases.planning import _parse_planning_output

        raw = {
            "microtasks": [
                {"id": "mt-1", "title": "Create models", "tool_agent": "data_engineering", "description": "define models"},
                {"id": "mt-2", "title": "Create API", "tool_agent": "api_openapi", "description": "routes", "depends_on": ["mt-1"]},
            ],
            "language": "python",
            "summary": "Plan created",
        }
        result = _parse_planning_output(raw, "python")
        assert len(result.microtasks) == 2
        assert result.microtasks[0].tool_agent == ToolAgentKind.DATA_ENGINEERING
        assert result.microtasks[1].depends_on == ["mt-1"]

    def test_run_planning_fallback(self, tmp_path):
        from backend_code_v2_team.phases.planning import run_planning

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## MICROTASKS ##\n## END MICROTASKS ##\n"
            "## LANGUAGE ##\npython\n## END LANGUAGE ##\n"
            "## SUMMARY ##\nempty\n## END SUMMARY ##"
        )
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build something")
        result = run_planning(llm=mock_llm, task=task, repo_path=tmp_path)
        assert len(result.microtasks) == 1
        assert result.microtasks[0].id == "mt-implement-task"


# ---------------------------------------------------------------------------
# Execution phase tests
# ---------------------------------------------------------------------------

class TestExecutionPhase:
    def test_run_execution_with_tool_runners(self, tmp_path):
        from backend_code_v2_team.phases.execution import run_execution

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build")

        def fake_runner(inp):
            return ToolAgentOutput(files={"models.py": "class User: pass"}, summary="done")

        planning = PlanningResult(
            microtasks=[Microtask(id="mt-1", tool_agent=ToolAgentKind.DATA_ENGINEERING, description="models")],
            language="python",
        )
        result = run_execution(
            llm=mock_llm,
            task=task,
            planning_result=planning,
            repo_path=tmp_path,
            tool_runners={ToolAgentKind.DATA_ENGINEERING: fake_runner},
        )
        assert "models.py" in result.files
        assert result.microtasks[0].status == MicrotaskStatus.COMPLETED

    def test_run_execution_general_fallback(self, tmp_path):
        from backend_code_v2_team.phases.execution import run_execution

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## FILE app.py ##\nprint('hello')\n## SUMMARY ##\ndone\n## END SUMMARY ##"
        )
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build")
        planning = PlanningResult(
            microtasks=[Microtask(id="mt-gen", tool_agent=ToolAgentKind.GENERAL, description="general task")],
            language="python",
        )
        result = run_execution(llm=mock_llm, task=task, planning_result=planning, repo_path=tmp_path)
        assert "app.py" in result.files


# ---------------------------------------------------------------------------
# Review phase tests
# ---------------------------------------------------------------------------

class TestReviewPhase:
    def test_review_passes_no_issues(self, tmp_path):
        from backend_code_v2_team.phases.review import run_review

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## PASSED ##\ntrue\n## END PASSED ##\n"
            "## ISSUES ##\n## END ISSUES ##\n"
            "## SUMMARY ##\nall good\n## END SUMMARY ##"
        )
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build")
        exec_result = ExecutionResult(files={"app.py": "print()"}, microtasks=[])
        result = run_review(llm=mock_llm, task=task, execution_result=exec_result, repo_path=tmp_path)
        assert result.passed
        assert result.build_ok

    def test_review_fails_on_critical_issues(self, tmp_path):
        from backend_code_v2_team.phases.review import run_review

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## PASSED ##\nfalse\n## END PASSED ##\n"
            "## ISSUES ##\n---\nsource: code_review\nseverity: critical\ndescription: SQL injection\nfile_path: \nrecommendation: \n---\n## END ISSUES ##\n"
            "## SUMMARY ##\ncritical issue\n## END SUMMARY ##"
        )
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build")
        exec_result = ExecutionResult(files={"app.py": "query(input)"}, microtasks=[])
        result = run_review(llm=mock_llm, task=task, execution_result=exec_result, repo_path=tmp_path)
        assert not result.passed


# ---------------------------------------------------------------------------
# Problem-solving phase tests
# ---------------------------------------------------------------------------

class TestProblemSolvingPhase:
    def test_no_actionable_issues(self):
        from backend_code_v2_team.phases.problem_solving import run_problem_solving

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build")
        review = ReviewResult(passed=False, issues=[
            ReviewIssue(source="code_review", severity="info", description="minor style"),
        ])
        result = run_problem_solving(llm=mock_llm, task=task, review_result=review, current_files={"a.py": "pass"})
        assert result.resolved

    def test_applies_fixes(self):
        from backend_code_v2_team.phases.problem_solving import run_problem_solving

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## FILE a.py ##\nfixed_code()\n"
            "## FIXES_APPLIED ##\n---\nissue: bug\nfix: fixed\n---\n## END FIXES_APPLIED ##\n"
            "## RESOLVED ##\ntrue\n## END RESOLVED ##\n"
            "## SUMMARY ##\nFixed bug\n## END SUMMARY ##"
        )
        task = Task(id="t1", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="build")
        review = ReviewResult(passed=False, issues=[
            ReviewIssue(source="code_review", severity="high", description="null pointer"),
        ])
        result = run_problem_solving(llm=mock_llm, task=task, review_result=review, current_files={"a.py": "bad_code()"})
        assert result.resolved
        assert result.files["a.py"] == "fixed_code()"


# ---------------------------------------------------------------------------
# Tool agents tests
# ---------------------------------------------------------------------------

class TestToolAgents:
    def test_data_engineering_agent(self):
        from backend_code_v2_team.tool_agents.data_engineering import DataEngineeringToolAgent

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## FILE models.py ##\nclass User: pass\n## SUMMARY ##\nschema done\n## END SUMMARY ##"
        )
        agent = DataEngineeringToolAgent(mock_llm)
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="create schema"), language="python")
        out = agent.run(inp)
        assert "models.py" in out.files

    def test_api_openapi_agent(self):
        from backend_code_v2_team.tool_agents.api_openapi import ApiOpenApiToolAgent

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## FILE routes.py ##\nroute()\n## SUMMARY ##\napi done\n## END SUMMARY ##"
        )
        agent = ApiOpenApiToolAgent(mock_llm)
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="create endpoint"), language="python")
        out = agent.run(inp)
        assert "routes.py" in out.files

    def test_auth_agent(self):
        from backend_code_v2_team.tool_agents.auth import AuthToolAgent

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = (
            "## FILE auth.py ##\ndef login(): pass\n## SUMMARY ##\nauth done\n## END SUMMARY ##"
        )
        agent = AuthToolAgent(mock_llm)
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="add login"), language="python")
        out = agent.run(inp)
        assert "auth.py" in out.files

    def test_cicd_stub(self):
        from backend_code_v2_team.tool_agents.cicd import CicdAdapterAgent

        agent = CicdAdapterAgent()
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="cicd"), language="python")
        out = agent.run(inp)
        assert not out.files
        assert out.summary

    def test_containerization_stub(self):
        from backend_code_v2_team.tool_agents.containerization import ContainerizationAdapterAgent

        agent = ContainerizationAdapterAgent()
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="docker"), language="python")
        out = agent.run(inp)
        assert not out.files
        assert out.summary

    def test_git_branch_management_agent(self, tmp_path):
        from backend_code_v2_team.tool_agents.git_branch_management import (
            GitBranchManagementToolAgent,
        )

        agent = GitBranchManagementToolAgent()
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.DELIVER,
            task_id="t1",
            task_title="API",
            task_description="Build API",
            feature_branch_name=None,
        )
        out = agent.plan(phase_inp)
        assert out.recommendations
        assert out.success
        out = agent.review(phase_inp)
        assert out.summary
        out = agent.problem_solve(phase_inp)
        assert out.summary
        exec_out = agent.execute(ToolAgentInput(microtask=Microtask(id="mt-1"), language="python"))
        assert exec_out.summary

        create_ok, branch = agent.create_feature_branch(tmp_path, "t1", "API")
        assert not create_ok and branch is None

        from software_engineering_team.shared.git_utils import initialize_new_repo
        ok, _ = initialize_new_repo(tmp_path)
        assert ok
        create_ok, branch = agent.create_feature_branch(tmp_path, "t1", "API")
        assert create_ok and branch is not None
        assert "feature/" in branch

    def test_git_agent_commit_current_changes(self, tmp_path):
        from backend_code_v2_team.tool_agents.git_branch_management import (
            GitBranchManagementToolAgent,
        )

        from software_engineering_team.shared.git_utils import initialize_new_repo

        initialize_new_repo(tmp_path)
        (tmp_path / "foo.txt").write_text("hi")
        agent = GitBranchManagementToolAgent()
        ok, msg = agent.commit_current_changes(tmp_path, "chore: add foo")
        assert ok

    def test_git_agent_deliver_with_feature_branch_name(self, tmp_path):
        from backend_code_v2_team.tool_agents.git_branch_management import (
            GitBranchManagementToolAgent,
        )

        from software_engineering_team.shared.git_utils import (
            create_feature_branch,
            initialize_new_repo,
        )

        initialize_new_repo(tmp_path)
        ok, branch = create_feature_branch(tmp_path, "development", "t1-api")
        assert ok and branch
        phase_inp = ToolAgentPhaseInput(
            phase=Phase.DELIVER,
            repo_path=str(tmp_path),
            task_id="t1",
            task_title="API",
            feature_branch_name=branch,
        )
        agent = GitBranchManagementToolAgent()
        out = agent.deliver(phase_inp)
        assert out.success
        assert "Merged" in out.summary

    def test_build_specialist_stub(self):
        from backend_code_v2_team.tool_agents.build_specialist import BuildSpecialistAdapterAgent

        agent = BuildSpecialistAdapterAgent()
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="build"), language="python")
        out = agent.run(inp)
        assert out.summary
        phase_inp = ToolAgentPhaseInput(phase=Phase.REVIEW)
        assert agent.plan(phase_inp).summary
        # review() returns issues when build is run; when repo_path is missing it returns a skip summary
        assert agent.review(phase_inp).summary
        assert agent.problem_solve(phase_inp).summary
        assert agent.deliver(phase_inp).summary

    def test_tool_agents_have_plan_review_problem_solve_deliver(self):
        """Tool agents participate in all phases: plan, execute, review, problem_solve, deliver."""
        from backend_code_v2_team.tool_agents.cicd import CicdAdapterAgent
        from backend_code_v2_team.tool_agents.data_engineering import DataEngineeringToolAgent

        mock_llm = MagicMock()
        data_eng = DataEngineeringToolAgent(mock_llm)
        inp = ToolAgentPhaseInput(phase=Phase.PLANNING, task_title="API", task_description="Build API")
        out = data_eng.plan(inp)
        assert out.recommendations
        assert out.success

        cicd = CicdAdapterAgent()
        rev_out = cicd.review(inp)
        assert rev_out.summary
        ps_out = cicd.problem_solve(inp)
        assert ps_out.summary
        del_out = cicd.deliver(inp)
        assert del_out.summary

    def test_data_engineering_execute_via_run(self):
        """run() delegates to execute() for backward compatibility."""
        from backend_code_v2_team.tool_agents.data_engineering import DataEngineeringToolAgent

        mock_llm = MagicMock()
        mock_llm.complete_text.return_value = "## FILE x.py ##\ncode\n## SUMMARY ##\ndone\n## END SUMMARY ##"
        agent = DataEngineeringToolAgent(mock_llm)
        inp = ToolAgentInput(microtask=Microtask(id="mt-1", description="schema"), language="python")
        out = agent.run(inp)
        assert out.files
        out2 = agent.execute(inp)
        assert out2.files == out.files


# ---------------------------------------------------------------------------
# BackendDevelopmentAgent tests (5-phase cycle)
# ---------------------------------------------------------------------------

class TestBackendDevelopmentAgent:
    def test_read_repo_code(self, tmp_path):
        from backend_code_v2_team.orchestrator import BackendDevelopmentAgent

        (tmp_path / "app.py").write_text("print('hello')")
        (tmp_path / "readme.md").write_text("# Readme")
        code = BackendDevelopmentAgent._read_repo_code(tmp_path)
        assert "app.py" in code
        assert "print('hello')" in code

    def test_build_tool_runners(self):
        from backend_code_v2_team.orchestrator import BackendDevelopmentAgent, _build_tool_agents

        mock_llm = MagicMock()
        dev = BackendDevelopmentAgent(mock_llm)
        tool_agents = _build_tool_agents(mock_llm)
        runners = dev._build_tool_runners(tool_agents)
        assert ToolAgentKind.DATA_ENGINEERING in runners
        assert ToolAgentKind.API_OPENAPI in runners
        assert ToolAgentKind.AUTH in runners
        assert ToolAgentKind.GIT_BRANCH_MANAGEMENT in tool_agents
        assert ToolAgentKind.BUILD_SPECIALIST in tool_agents
        git_agent = tool_agents[ToolAgentKind.GIT_BRANCH_MANAGEMENT]
        assert hasattr(git_agent, "create_feature_branch")
        assert hasattr(git_agent, "commit_current_changes")
        assert hasattr(git_agent, "deliver")


# ---------------------------------------------------------------------------
# BackendCodeV2TeamLead (Tech Lead: Setup + delegate) tests
# ---------------------------------------------------------------------------

class TestBackendCodeV2TeamLead:
    def test_team_lead_runs_setup_then_delegates(self, tmp_path):
        """BackendCodeV2TeamLead runs Setup then delegates to BackendDevelopmentAgent."""
        from backend_code_v2_team.orchestrator import BackendCodeV2TeamLead

        from software_engineering_team.shared.models import Task, TaskStatus, TaskType

        mock_llm = MagicMock()
        planning_response = (
            "## MICROTASKS ##\n---\nid: mt-1\ntitle: A\ndescription: a\ntool_agent: general\ndepends_on: \n---\n## END MICROTASKS ##\n"
            "## LANGUAGE ##\npython\n## END LANGUAGE ##\n## SUMMARY ##\nplan\n## END SUMMARY ##"
        )
        execution_response = "## SUMMARY ##\nnothing\n## END SUMMARY ##"
        mock_llm.complete_text.side_effect = [planning_response, execution_response]

        lead = BackendCodeV2TeamLead(mock_llm)
        task = Task(id="t-test", type=TaskType.BACKEND, assignee="backend-code-v2", status=TaskStatus.PENDING, description="Do something")

        (tmp_path / ".git").mkdir()

        result = lead.run_workflow(repo_path=tmp_path, task=task)
        assert result.setup_result is not None
        assert not result.success
        assert "no files" in result.failure_reason.lower() or result.failure_reason != ""
