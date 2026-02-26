"""
Unit tests for planning_v2_team: 3-layer architecture with 8 tool agents.

Tests:
- Tool agent instantiation
- Tool agent phase methods
- Orchestrator 3-layer architecture
- Phase invocation with tool agents
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
    SpecReviewResult,
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
        """Test plan method creates hierarchy."""
        from planning_v2_team.tool_agents.user_story import UserStoryToolAgent
        
        mock_llm.complete_json.return_value = {
            "initiatives": [
                {
                    "id": "INIT-1",
                    "title": "Test Initiative",
                    "description": "Test description",
                    "epics": [
                        {
                            "id": "EPIC-1",
                            "title": "Test Epic",
                            "description": "Epic description",
                            "acceptance_criteria": ["AC1"],
                            "stories": [
                                {
                                    "id": "STORY-1",
                                    "title": "Test Story",
                                    "description": "Story description",
                                    "acceptance_criteria": ["Story AC"],
                                    "tasks": [
                                        {
                                            "id": "TASK-1",
                                            "title": "Test Task",
                                            "description": "Task description",
                                            "assigned_team": "backend",
                                            "assignee": "backend",
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ],
            "summary": "Created hierarchy"
        }
        
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
    
    def test_spec_review_has_system_design_and_architecture(self):
        """Spec Review phase should have System Design and Architecture."""
        agents = PHASE_TOOL_AGENTS[Phase.SPEC_REVIEW_GAP]
        assert ToolAgentKind.SYSTEM_DESIGN in agents
        assert ToolAgentKind.ARCHITECTURE in agents
    
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

        plan_dir = temp_repo / "planning_v2"
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
        
        assert result.spec_review_result is not None
        assert result.planning_result is not None
        assert result.implementation_result is not None


# ---------------------------------------------------------------------------
# Phase Tests
# ---------------------------------------------------------------------------


class TestSpecReviewPhase:
    """Tests for spec_review_gap phase."""
    
    def test_invokes_tool_agents(self, mock_llm: MagicMock, temp_repo: Path):
        """Test spec review invokes System Design and Architecture agents."""
        from planning_v2_team.phases.spec_review_gap import run_spec_review_gap
        
        tool_agents = _build_tool_agents(mock_llm)
        
        result = run_spec_review_gap(
            llm=mock_llm,
            spec_content="Test spec",
            repo_path=temp_repo,
            tool_agents=tool_agents,
        )
        
        assert isinstance(result, SpecReviewResult)


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
        assert (temp_repo / "planning_v2").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
