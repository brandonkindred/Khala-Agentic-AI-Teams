"""Tests for Deepthought Pydantic models."""

from deepthought.models import (
    AgentResult,
    AgentSpec,
    DeepthoughtRequest,
    DeepthoughtResponse,
    QueryAnalysis,
    SkillRequirement,
)


def test_skill_requirement_roundtrip():
    sr = SkillRequirement(
        name="physics_expert",
        description="Expert in classical mechanics",
        focus_question="What is Newton's second law?",
        reasoning="The question involves force and acceleration",
    )
    data = sr.model_dump()
    restored = SkillRequirement(**data)
    assert restored.name == "physics_expert"


def test_query_analysis_direct():
    qa = QueryAnalysis(
        summary="Simple question",
        can_answer_directly=True,
        direct_answer="42",
        confidence=0.95,
        skill_requirements=[],
    )
    assert qa.can_answer_directly
    assert qa.direct_answer == "42"
    assert qa.skill_requirements == []


def test_query_analysis_decompose():
    qa = QueryAnalysis(
        summary="Complex question",
        can_answer_directly=False,
        direct_answer=None,
        confidence=0.0,
        skill_requirements=[
            SkillRequirement(
                name="econ", description="Economist", focus_question="GDP?", reasoning="needed"
            )
        ],
    )
    assert not qa.can_answer_directly
    assert len(qa.skill_requirements) == 1


def test_agent_spec_creation():
    spec = AgentSpec(
        agent_id="abc-123",
        name="test_agent",
        role_description="Test role",
        focus_question="What is X?",
        depth=3,
        parent_id="parent-1",
    )
    assert spec.depth == 3
    assert spec.parent_id == "parent-1"


def test_agent_result_recursive():
    child = AgentResult(
        agent_id="child-1",
        agent_name="child",
        depth=1,
        focus_question="Sub-question?",
        answer="Sub-answer",
        confidence=0.8,
        child_results=[],
        was_decomposed=False,
    )
    parent = AgentResult(
        agent_id="parent-1",
        agent_name="parent",
        depth=0,
        focus_question="Main question?",
        answer="Synthesised answer",
        confidence=0.85,
        child_results=[child],
        was_decomposed=True,
    )
    assert parent.was_decomposed
    assert len(parent.child_results) == 1
    assert parent.child_results[0].agent_name == "child"

    # Verify JSON roundtrip preserves nested structure
    data = parent.model_dump()
    restored = AgentResult(**data)
    assert len(restored.child_results) == 1
    assert restored.child_results[0].answer == "Sub-answer"


def test_deepthought_request_defaults():
    req = DeepthoughtRequest(message="Hello")
    assert req.max_depth == 10
    assert req.conversation_history == []


def test_deepthought_request_custom():
    req = DeepthoughtRequest(
        message="Complex query",
        max_depth=5,
        conversation_history=[{"role": "user", "content": "prior msg"}],
    )
    assert req.max_depth == 5
    assert len(req.conversation_history) == 1


def test_deepthought_response_serialisation():
    tree = AgentResult(
        agent_id="root",
        agent_name="root_agent",
        depth=0,
        focus_question="Q?",
        answer="A",
        confidence=0.9,
        child_results=[],
        was_decomposed=False,
    )
    resp = DeepthoughtResponse(
        answer="Final answer",
        agent_tree=tree,
        total_agents_spawned=1,
        max_depth_reached=0,
    )
    data = resp.model_dump()
    assert data["total_agents_spawned"] == 1
    assert data["agent_tree"]["agent_name"] == "root_agent"
