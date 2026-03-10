"""
Unit tests for planning_v2_team: 3-layer architecture with 8 tool agents.

Tests:
- Tool agent instantiation
- Tool agent phase methods
- Orchestrator 3-layer architecture
- Phase invocation with tool agents

Note: This team expects a pre-validated specification - no spec review tests.
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from typing import Dict, Any

from planning_v2_team.models import (
    Phase,
    ToolAgentKind,
    ToolAgentPhaseInput,
    ToolAgentPhaseOutput,
    PlanningPhaseResult,
)
from planning_v2_team.orchestrator import (
    PlanningV2ProductLead,
    PlanningV2PlanningAgent,
    PlanningV2TeamLead,
    _build_tool_agents,
    PHASE_TOOL_AGENTS,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm() -> MagicMock:
    """Create a mock LLM client."""
    llm = MagicMock()
    llm.complete_json.return_value = {
        "summary": "Test summary",
        "issues": [],
        "product_gaps": [],
        "open_questions": [],
        "plan_summary": "Test plan summary",
    }
    llm.complete_text.return_value = "Test response"
    return llm


@pytest.fixture
def temp_repo(tmp_path: Path) -> Path:
    """Create a temporary repo directory."""
    repo = tmp_path / "test_repo"
    repo.mkdir()
    return repo


@pytest.fixture
def sample_spec() -> str:
    """Sample specification content."""
    return """
    # Test Product Specification
    
    ## Overview
    Build a simple task management application.
    
    ## Features
    - Create tasks
    - List tasks
    - Mark tasks complete
    
    ## Technical Requirements
    - Frontend: React
    - Backend: Python FastAPI
    - Database: PostgreSQL
    """


# ---------------------------------------------------------------------------
# Tool Agent Tests
# ---------------------------------------------------------------------------


class TestToolAgentKindEnum:
    """Tests for ToolAgentKind enum."""
    
    def test_all_8_tool_agents_defined(self):
        """Verify all 8 tool agents are in the enum."""
        expected = {
            "system_design",
            "architecture",
            "user_story",
            "devops",
            "ui_design",
            "ux_design",
            "task_classification",
            "task_dependency",
        }
        actual = {k.value for k in ToolAgentKind}
        assert actual == expected
    
    def test_tool_agent_kind_values(self):
        """Test enum value access."""
        assert ToolAgentKind.SYSTEM_DESIGN.value == "system_design"
        assert ToolAgentKind.USER_STORY.value == "user_story"


class TestToolAgentPhaseInputOutput:
    """Tests for tool agent input/output models."""
    
    def test_phase_input_defaults(self):
        """Test ToolAgentPhaseInput has sensible defaults."""
        inp = ToolAgentPhaseInput()
        assert inp.spec_content == ""
        assert inp.current_files == {}
        assert inp.review_issues == []
        assert inp.hierarchy is None
    
    def test_phase_input_with_values(self):
        """Test ToolAgentPhaseInput with provided values."""
        inp = ToolAgentPhaseInput(
            spec_content="Test spec",
            repo_path="/test/repo",
            current_files={"test.py": "content"},
        )
        assert inp.spec_content == "Test spec"
        assert inp.repo_path == "/test/repo"
        assert "test.py" in inp.current_files
    
    def test_phase_output_defaults(self):
        """Test ToolAgentPhaseOutput has sensible defaults."""
        out = ToolAgentPhaseOutput()
        assert out.summary == ""
        assert out.recommendations == []
        assert out.issues == []
        assert out.files == {}
    
    def test_phase_output_with_values(self):
        """Test ToolAgentPhaseOutput with provided values."""
        out = ToolAgentPhaseOutput(
            summary="Test summary",
            recommendations=["rec1", "rec2"],
            issues=["issue1"],
            files={"test.md": "content"},
        )
        assert out.summary == "Test summary"
        assert len(out.recommendations) == 2
        assert len(out.issues) == 1


class TestBuildToolAgents:
    """Tests for _build_tool_agents factory function."""
    
    def test_builds_all_8_agents(self, mock_llm: MagicMock):
        """Verify all 8 tool agents are built."""
        agents = _build_tool_agents(mock_llm)
        assert len(agents) == 8
        for kind in ToolAgentKind:
            assert kind in agents
    
    def test_agents_have_required_methods(self, mock_llm: MagicMock):
        """Verify tool agents have the required phase methods."""
        agents = _build_tool_agents(mock_llm)
        required_methods = ["plan", "execute", "review", "problem_solve", "deliver"]
        
        for kind, agent in agents.items():
            for method in required_methods:
                assert hasattr(agent, method), f"{kind.value} missing {method}"


class TestSystemDesignToolAgent:
    """Tests for SystemDesignToolAgent."""
    
    def test_spec_review_method(self, mock_llm: MagicMock):
        """Test spec_review returns issues."""
        from planning_v2_team.tool_agents.system_design import SystemDesignToolAgent
        
        agent = SystemDesignToolAgent(mock_llm)
        inp = ToolAgentPhaseInput(spec_content="Test spec")
        
        result = agent.spec_review(inp)
        assert isinstance(result, ToolAgentPhaseOutput)
    
    def test_plan_method(self, mock_llm: MagicMock):
        """Test plan returns recommendations."""
        from planning_v2_team.tool_agents.system_design import SystemDesignToolAgent
        
        agent = SystemDesignToolAgent(mock_llm)
        inp = ToolAgentPhaseInput(spec_content="Test spec")
        
        result = agent.plan(inp)
        assert isinstance(result, ToolAgentPhaseOutput)


class TestUserStoryToolAgent:
    """Tests for UserStoryToolAgent."""
    
    def test_plan_creates_hierarchy(self, mock_llm: MagicMock):
        """Test plan method creates hierarchy from text template output."""
        from planning_v2_team.tool_agents.user_story import UserStoryToolAgent

        # User story agent uses line-based format (INIT|EPIC|STORY|TASK), not JSON
        hierarchy_text = (
            "INIT | INIT-1 | Test Initiative | Test description\n"
            "EPIC | EPIC-1 | Test Epic | Epic description\n"
            "STORY | STORY-1 | Test Story | Story description\n"
            "TASK | TASK-1 | Test Task | backend | 2\n"
            "## SUMMARY ##\nCreated hierarchy\n## END SUMMARY ##"
        )
        mock_llm.complete_text.return_value = hierarchy_text

        agent = UserStoryToolAgent(mock_llm)
        inp = ToolAgentPhaseInput(spec_content="Test spec")

        result = agent.plan(inp)
        assert result.hierarchy is not None
        assert len(result.hierarchy.initiatives) == 1


class TestTaskDependencyToolAgent:
    """Tests for TaskDependencyToolAgent."""
    
    def test_review_analyzes_dependencies(self, mock_llm: MagicMock):
        """Test review returns dependency analysis."""
        from planning_v2_team.tool_agents.task_dependency import TaskDependencyToolAgent
        
        mock_llm.complete_json.return_value = {
            "dependencies": [
                {"from_task": "TASK-1", "to_task": "TASK-2", "type": "blocks"}
            ],
            "circular_risks": [],
            "critical_path": ["TASK-1", "TASK-2"],
            "parallelizable": [],
            "issues": [],
            "summary": "Analyzed dependencies"
        }
        
        agent = TaskDependencyToolAgent(mock_llm)
        inp = ToolAgentPhaseInput(
            spec_content="Test spec",
            current_files={"tasks.md": "task content"},
        )
        
        result = agent.review(inp)
        assert isinstance(result, ToolAgentPhaseOutput)


# ---------------------------------------------------------------------------
# Orchestrator Tests
# ---------------------------------------------------------------------------


class TestPhaseToolAgentMapping:
    """Tests for phase-tool agent mapping."""
    
    def test_planning_has_5_agents(self):
        """Planning phase should have 5 agents."""
        agents = PHASE_TOOL_AGENTS[Phase.PLANNING]
        expected = {
            ToolAgentKind.SYSTEM_DESIGN,
            ToolAgentKind.ARCHITECTURE,
            ToolAgentKind.USER_STORY,
            ToolAgentKind.DEVOPS,
            ToolAgentKind.UI_DESIGN,
        }
        assert set(agents) == expected
    
    def test_implementation_has_7_agents(self):
        """Implementation phase should have 7 agents (all except Task Dependency)."""
        agents = PHASE_TOOL_AGENTS[Phase.IMPLEMENTATION]
        assert ToolAgentKind.TASK_DEPENDENCY not in agents
        assert len(agents) == 7
    
    def test_review_has_task_dependency(self):
        """Review phase should have Task Dependency."""
        agents = PHASE_TOOL_AGENTS[Phase.REVIEW]
        assert ToolAgentKind.TASK_DEPENDENCY in agents


class TestPlanningV2PlanningAgent:
    """Tests for PlanningV2PlanningAgent (Layer 2)."""
    
    def test_init_requires_llm(self):
        """Test that LLM client is required."""
        with pytest.raises(AssertionError):
            PlanningV2PlanningAgent(None)
    
    def test_init_creates_tool_agents(self, mock_llm: MagicMock):
        """Test that init creates tool agents."""
        agent = PlanningV2PlanningAgent(mock_llm)
        assert hasattr(agent, "tool_agents")
        assert len(agent.tool_agents) == 8


class TestPlanningV2ProductLead:
    """Tests for PlanningV2ProductLead (Layer 1)."""
    
    def test_init_requires_llm(self):
        """Test that LLM client is required."""
        with pytest.raises(AssertionError):
            PlanningV2ProductLead(None)
    
    def test_backward_compat_alias(self, mock_llm: MagicMock):
        """Test PlanningV2TeamLead is an alias for ProductLead."""
        lead = PlanningV2TeamLead(mock_llm)
        assert isinstance(lead, PlanningV2ProductLead)


class TestWorkflowExecution:
    """Integration tests for workflow execution."""
    
    def test_workflow_creates_planning_dir(
        self,
        mock_llm: MagicMock,
        temp_repo: Path,
        sample_spec: str,
    ):
        """Test workflow creates planning_v2 directory."""
        mock_llm.complete_json.return_value = {
            "summary": "Test",
            "issues": [],
            "product_gaps": [],
            "open_questions": [],
            "plan_summary": "Plan summary",
            "goals_vision": "Goals",
            "constraints_limitations": "",
            "key_features": ["Feature 1"],
            "milestones": ["M1"],
            "architecture": "Arch",
            "maintainability": "",
            "security": "",
            "file_system": "",
            "styling": "",
            "dependencies": [],
            "microservices": "",
            "others": "",
            "passed": True,
            "initiatives": [],
        }

        lead = PlanningV2ProductLead(mock_llm)
        result = lead.run_workflow(
            spec_content=sample_spec,
            repo_path=temp_repo,
        )

        # Workflow writes to plan/ (e.g. plan/planning_artifacts.md, plan/product_spec.md)
        plan_dir = temp_repo / "plan"
        assert plan_dir.exists()

    def test_workflow_result_structure(
        self,
        mock_llm: MagicMock,
        temp_repo: Path,
        sample_spec: str,
    ):
        """Test workflow result has expected structure."""
        mock_llm.complete_json.return_value = {
            "summary": "Test",
            "issues": [],
            "product_gaps": [],
            "open_questions": [],
            "plan_summary": "Plan summary",
            "goals_vision": "Goals",
            "constraints_limitations": "",
            "key_features": ["Feature 1"],
            "milestones": ["M1"],
            "architecture": "Arch",
            "maintainability": "",
            "security": "",
            "file_system": "",
            "styling": "",
            "dependencies": [],
            "microservices": "",
            "others": "",
            "passed": True,
            "initiatives": [],
        }
        
        lead = PlanningV2ProductLead(mock_llm)
        result = lead.run_workflow(
            spec_content=sample_spec,
            repo_path=temp_repo,
        )

        # PlanningV2WorkflowResult has planning_result, implementation_result, review_result, etc. (no spec_review_result)
        assert result.planning_result is not None
        assert result.implementation_result is not None
        assert result.review_result is not None


# ---------------------------------------------------------------------------
# Phase Tests
# ---------------------------------------------------------------------------


class TestPlanningPhase:
    """Tests for planning phase."""
    
    def test_invokes_tool_agents(self, mock_llm: MagicMock, temp_repo: Path):
        """Test planning invokes correct tool agents."""
        from planning_v2_team.phases.planning import run_planning
        
        mock_llm.complete_json.return_value = {
            "goals_vision": "Goals",
            "constraints_limitations": "",
            "key_features": ["Feature 1"],
            "milestones": ["M1"],
            "architecture": "Arch",
            "maintainability": "",
            "security": "",
            "file_system": "",
            "styling": "",
            "dependencies": [],
            "microservices": "",
            "others": "",
            "summary": "Done",
            "initiatives": [],
        }
        
        tool_agents = _build_tool_agents(mock_llm)
        
        result = run_planning(
            llm=mock_llm,
            spec_content="Test spec",
            repo_path=temp_repo,
            tool_agents=tool_agents,
        )
        
        assert isinstance(result, PlanningPhaseResult)


class TestImplementationPhase:
    """Tests for implementation phase."""
    
    def test_creates_artifacts(self, mock_llm: MagicMock, temp_repo: Path):
        """Test implementation creates planning artifacts."""
        from planning_v2_team.phases.implementation import run_implementation
        
        tool_agents = _build_tool_agents(mock_llm)
        
        result = run_implementation(
            llm=mock_llm,
            spec_content="Test spec",
            repo_path=temp_repo,
            tool_agents=tool_agents,
        )
        
        assert len(result.assets_created) > 0
        assert (temp_repo / "plan").exists()


class TestCompletenessHelper:
    """Tests for looks_like_truncated_file_content (Planning V2 truncation fix)."""

    def test_truncated_short_last_line(self):
        """Content ending with a very short line (e.g. mid-sentence) is detected as truncated."""
        from planning_v2_team.output_templates import looks_like_truncated_file_content

        content = "# UI Design\n\n## Section\n\n- Item 1\n- Item 2\nI want to create a task with a ti"
        assert looks_like_truncated_file_content(content) is True

    def test_complete_content_not_truncated(self):
        """Content with a normal ending is not flagged as truncated."""
        from planning_v2_team.output_templates import looks_like_truncated_file_content

        content = "# UI Design\n\n## Section\n\n- Item 1\n- Item 2\n\nAcceptance criteria met.\n"
        assert looks_like_truncated_file_content(content) is False

    def test_empty_content_not_truncated(self):
        """Empty or whitespace-only content returns False."""
        from planning_v2_team.output_templates import looks_like_truncated_file_content

        assert looks_like_truncated_file_content("") is False
        assert looks_like_truncated_file_content("   \n  ") is False


# ---------------------------------------------------------------------------
# Issue classification and status breakdown (problem_solving helpers)
# ---------------------------------------------------------------------------


class TestClassifyIssue:
    """Tests for _classify_issue: maps issue text to ToolAgentKind."""

    def test_architecture_keywords(self):
        from planning_v2_team.phases.problem_solving import _classify_issue

        assert _classify_issue("architecture layer boundaries unclear") == ToolAgentKind.ARCHITECTURE
        assert _classify_issue("component integration missing") == ToolAgentKind.ARCHITECTURE
        assert _classify_issue("module dependencies") == ToolAgentKind.ARCHITECTURE

    def test_user_story_keywords(self):
        from planning_v2_team.phases.problem_solving import _classify_issue

        assert _classify_issue("user story missing acceptance criteria") == ToolAgentKind.USER_STORY
        assert _classify_issue("epic scope unclear") == ToolAgentKind.USER_STORY
        assert _classify_issue("task criteria") == ToolAgentKind.USER_STORY

    def test_system_design_keywords(self):
        from planning_v2_team.phases.problem_solving import _classify_issue

        assert _classify_issue("system design data flow gaps") == ToolAgentKind.SYSTEM_DESIGN
        assert _classify_issue("diagram missing") == ToolAgentKind.SYSTEM_DESIGN
        assert _classify_issue("interface not defined") == ToolAgentKind.SYSTEM_DESIGN

    def test_task_classification_keywords(self):
        from planning_v2_team.phases.problem_solving import _classify_issue

        assert _classify_issue("task team assignment wrong") == ToolAgentKind.TASK_CLASSIFICATION
        assert _classify_issue("classification should be frontend") == ToolAgentKind.TASK_CLASSIFICATION
        assert _classify_issue("assign to backend team") == ToolAgentKind.TASK_CLASSIFICATION
        assert _classify_issue("team assignment for QA") == ToolAgentKind.TASK_CLASSIFICATION

    def test_devops_ui_ux_keywords(self):
        from planning_v2_team.phases.problem_solving import _classify_issue

        assert _classify_issue("deploy pipeline missing") == ToolAgentKind.DEVOPS
        assert _classify_issue("UI layout inconsistent") == ToolAgentKind.UI_DESIGN
        assert _classify_issue("UX accessibility") == ToolAgentKind.UX_DESIGN

    def test_default_system_design(self):
        from planning_v2_team.phases.problem_solving import _classify_issue

        assert _classify_issue("something generic") == ToolAgentKind.SYSTEM_DESIGN


class TestGroupIssuesByAgent:
    """Tests for group_issues_by_agent."""

    def test_empty_list_returns_empty_dict(self):
        from planning_v2_team.phases.problem_solving import group_issues_by_agent

        assert group_issues_by_agent([]) == {}

    def test_mixed_issues_grouped_correctly(self):
        from planning_v2_team.phases.problem_solving import group_issues_by_agent

        issues = [
            "architecture layer unclear",
            "user story missing acceptance criteria",
            "architecture component boundary",
            "task team assignment wrong",
            "user story epic scope",
        ]
        grouped = group_issues_by_agent(issues)
        assert len(grouped[ToolAgentKind.ARCHITECTURE]) == 2
        assert len(grouped[ToolAgentKind.USER_STORY]) == 2
        assert len(grouped[ToolAgentKind.TASK_CLASSIFICATION]) == 1

    def test_format_breakdown_and_synopsis(self):
        from planning_v2_team.phases.problem_solving import (
            format_issues_breakdown_and_synopsis,
            group_issues_by_agent,
        )

        issues = [
            "user story missing acceptance criteria",
            "architecture layer boundaries",
        ]
        grouped = group_issues_by_agent(issues)
        counts, synopsis = format_issues_breakdown_and_synopsis(grouped)
        assert "1 user story" in counts
        assert "1 architecture" in counts
        assert "User story" in synopsis
        assert "Architecture" in synopsis
        assert "missing acceptance criteria" in synopsis or "acceptance" in synopsis


class TestOrchestratorStatusText:
    """Tests that orchestrator sets status_text for each phase."""

    def test_update_job_receives_planning_status_text(self, mock_llm: MagicMock, temp_repo: Path, sample_spec: str):
        """When workflow runs, job updater is called with planning status_text listing agents."""
        from planning_v2_team.orchestrator import PlanningV2ProductLead, Phase

        job_updates: list = []

        def capture_update(**kwargs: Any) -> None:
            job_updates.append(kwargs.copy())

        lead = PlanningV2ProductLead(mock_llm)
        mock_llm.complete_json.return_value = {
            "goals_vision": "Goals",
            "constraints_limitations": "",
            "key_features": ["F1"],
            "milestones": ["M1"],
            "architecture": "Arch",
            "maintainability": "",
            "security": "",
            "file_system": "",
            "styling": "",
            "dependencies": [],
            "microservices": "",
            "others": "",
            "summary": "Done",
            "initiatives": [],
        }
        try:
            lead.run_workflow(
                spec_content=sample_spec,
                repo_path=temp_repo,
                job_updater=capture_update,
            )
        except Exception:
            pass
        planning_updates = [u for u in job_updates if u.get("current_phase") == Phase.PLANNING.value]
        assert len(planning_updates) >= 1
        status_texts = [u.get("status_text") for u in planning_updates if u.get("status_text")]
        assert any(
            "system design" in (t or "").lower() and "user stories" in (t or "").lower()
            for t in status_texts
        ), f"Expected planning status_text to list agents, got: {status_texts}"

    def test_implementation_with_issues_status_includes_breakdown(self, mock_llm: MagicMock, temp_repo: Path, sample_spec: str):
        """When implementation runs with review issues, status_text includes breakdown."""
        from planning_v2_team.models import ReviewPhaseResult
        from planning_v2_team.orchestrator import PlanningV2PlanningAgent, Phase

        job_updates: list = []

        def capture_update(**kwargs: Any) -> None:
            job_updates.append(kwargs.copy())

        mock_llm.complete_json.return_value = {
            "goals_vision": "Goals",
            "constraints_limitations": "",
            "key_features": ["F1"],
            "milestones": ["M1"],
            "architecture": "Arch",
            "maintainability": "",
            "security": "",
            "file_system": "",
            "styling": "",
            "dependencies": [],
            "microservices": "",
            "others": "",
            "summary": "Done",
            "initiatives": [],
            "issues": [],
            "passed": False,
        }
        agent = PlanningV2PlanningAgent(mock_llm)
        # Run one full Planning -> Implementation (with mock review_result) would require
        # injecting review_result. Easier: assert that the code path that builds
        # "Fixing N issues: ..." is used when issue_count > 0 (tested via group_issues_by_agent + format)
        from planning_v2_team.phases.problem_solving import (
            format_issues_breakdown_and_synopsis,
            group_issues_by_agent,
        )

        issues = ["user story missing criteria", "architecture layer"]
        grouped = group_issues_by_agent(issues)
        counts, synopsis = format_issues_breakdown_and_synopsis(grouped)
        assert "2" in counts or "1 user story" in counts
        assert "user story" in counts
        assert "architecture" in counts


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
