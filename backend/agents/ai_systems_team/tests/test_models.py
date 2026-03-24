"""Tests for ai_systems_team models."""

from ai_systems_team.models import (
    KPI,
    AcceptanceTest,
    AgentBlueprint,
    AgentRole,
    AISystemJobResponse,
    AISystemJobsListResponse,
    AISystemRequest,
    AISystemStatusResponse,
    HandoffRule,
    OrchestrationPattern,
    Phase,
    SafetyCheckpoint,
    SpecIntakeResult,
    ToolContract,
)


def test_phase_enum_values():
    assert Phase.SPEC_INTAKE == "spec_intake"
    assert Phase.ARCHITECTURE == "architecture"
    assert Phase.CAPABILITIES == "capabilities"
    assert Phase.EVALUATION == "evaluation"
    assert Phase.SAFETY == "safety"
    assert Phase.BUILD == "build"


def test_orchestration_pattern_enum_values():
    assert OrchestrationPattern.SEQUENTIAL == "sequential"
    assert OrchestrationPattern.PARALLEL == "parallel"
    assert OrchestrationPattern.HIERARCHICAL == "hierarchical"
    assert OrchestrationPattern.EVENT_DRIVEN == "event_driven"
    assert OrchestrationPattern.HYBRID == "hybrid"


def test_agent_role_instantiation():
    role = AgentRole(name="planner", description="Plans tasks")
    assert role.name == "planner"
    assert role.capabilities == []
    assert role.tools == []


def test_agent_role_with_fields():
    role = AgentRole(
        name="executor",
        description="Executes tasks",
        capabilities=["code", "test"],
        tools=["bash", "python"],
        inputs=["task"],
        outputs=["result"],
    )
    assert len(role.capabilities) == 2
    assert "bash" in role.tools


def test_handoff_rule():
    rule = HandoffRule(from_agent="A", to_agent="B", condition="task_complete")
    assert rule.from_agent == "A"
    assert rule.to_agent == "B"
    assert rule.data_passed == []


def test_tool_contract():
    tc = ToolContract(name="search", description="Web search tool")
    assert tc.name == "search"
    assert tc.error_handling == ""
    assert tc.rate_limits is None


def test_safety_checkpoint():
    sc = SafetyCheckpoint(
        name="pii_check",
        description="Check for PII",
        trigger="before_output",
        action="redact",
    )
    assert sc.name == "pii_check"
    assert sc.requires_human_approval is False


def test_acceptance_test():
    at = AcceptanceTest(
        name="basic_test",
        description="Tests basic flow",
        input_scenario="user asks question",
        expected_outcome="agent answers",
        pass_criteria="answer is relevant",
    )
    assert at.name == "basic_test"


def test_kpi():
    kpi = KPI(
        name="accuracy",
        description="Answer accuracy",
        metric="accuracy_rate",
        target_value="0.9",
        measurement_method="eval_harness",
    )
    assert kpi.name == "accuracy"
    assert kpi.target_value == "0.9"


def test_agent_blueprint_defaults():
    bp = AgentBlueprint(project_name="my_system")
    assert bp.project_name == "my_system"
    assert bp.version == "1.0.0"
    assert bp.success is False
    assert bp.current_phase == Phase.SPEC_INTAKE
    assert bp.completed_phases == []


def test_agent_blueprint_with_phase_results():
    spec = SpecIntakeResult(success=True, goals=["build agent"])
    bp = AgentBlueprint(project_name="proj", spec_intake=spec)
    assert bp.spec_intake.success is True
    assert bp.spec_intake.goals == ["build agent"]


def test_ai_system_request():
    req = AISystemRequest(project_name="my_ai", spec_path="/path/to/spec.md")
    assert req.project_name == "my_ai"
    assert req.spec_path == "/path/to/spec.md"
    assert req.constraints == {}
    assert req.output_dir is None


def test_ai_system_job_response():
    resp = AISystemJobResponse(job_id="abc-123", status="running", message="started")
    assert resp.job_id == "abc-123"
    assert resp.status == "running"


def test_ai_system_status_response_defaults():
    resp = AISystemStatusResponse(job_id="j1", status="pending")
    assert resp.progress == 0
    assert resp.completed_phases == []
    assert resp.error is None


def test_ai_system_jobs_list_response_empty():
    resp = AISystemJobsListResponse()
    assert resp.jobs == []
