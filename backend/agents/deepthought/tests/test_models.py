"""Tests for Deepthought Pydantic models."""

from deepthought.models import (
    AgentEvent,
    AgentEventType,
    AgentResult,
    AgentSpec,
    DecompositionStrategy,
    DeepthoughtRequest,
    DeepthoughtResponse,
    KnowledgeEntry,
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


def test_agent_result_recursive_with_deliberation():
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
        deliberation_notes="No contradictions found.",
    )
    assert parent.was_decomposed
    assert parent.deliberation_notes == "No contradictions found."
    assert len(parent.child_results) == 1

    # Verify JSON roundtrip preserves nested structure
    data = parent.model_dump()
    restored = AgentResult(**data)
    assert len(restored.child_results) == 1
    assert restored.child_results[0].answer == "Sub-answer"
    assert restored.deliberation_notes is not None


def test_agent_result_reused_from_cache():
    result = AgentResult(
        agent_id="cached-1",
        agent_name="cached_agent",
        depth=2,
        focus_question="Cached Q?",
        answer="Cached answer",
        confidence=0.9,
        reused_from_cache=True,
    )
    assert result.reused_from_cache


def test_deepthought_request_defaults():
    req = DeepthoughtRequest(message="Hello")
    assert req.max_depth == 10
    assert req.conversation_history == []
    assert req.decomposition_strategy == DecompositionStrategy.AUTO


def test_deepthought_request_custom():
    req = DeepthoughtRequest(
        message="Complex query",
        max_depth=5,
        conversation_history=[{"role": "user", "content": "prior msg"}],
        decomposition_strategy=DecompositionStrategy.BY_CONCERN,
    )
    assert req.max_depth == 5
    assert len(req.conversation_history) == 1
    assert req.decomposition_strategy == DecompositionStrategy.BY_CONCERN


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
        knowledge_entries=[
            KnowledgeEntry(
                agent_id="root",
                agent_name="root_agent",
                focus_question="Q?",
                finding="A",
                confidence=0.9,
                tags=["root"],
            )
        ],
        events=[
            AgentEvent(
                event_type=AgentEventType.AGENT_COMPLETE,
                agent_id="root",
                agent_name="root_agent",
                depth=0,
                detail="done",
            )
        ],
    )
    data = resp.model_dump()
    assert data["total_agents_spawned"] == 1
    assert data["agent_tree"]["agent_name"] == "root_agent"
    assert len(data["knowledge_entries"]) == 1
    assert len(data["events"]) == 1


def test_decomposition_strategy_values():
    assert DecompositionStrategy.AUTO == "auto"
    assert DecompositionStrategy.BY_DISCIPLINE == "by_discipline"
    assert DecompositionStrategy.BY_CONCERN == "by_concern"
    assert DecompositionStrategy.BY_OPTION == "by_option"
    assert DecompositionStrategy.BY_PERSPECTIVE == "by_perspective"
    assert DecompositionStrategy.NONE == "none"


def test_agent_event_serialisation():
    event = AgentEvent(
        event_type=AgentEventType.AGENT_SPAWNED,
        agent_id="a1",
        agent_name="test",
        depth=3,
        detail="Spawned for testing",
    )
    data = event.model_dump()
    assert data["event_type"] == "agent_spawned"
    assert data["depth"] == 3


def test_knowledge_entry_tags():
    entry = KnowledgeEntry(
        agent_id="e1",
        agent_name="physics_expert",
        focus_question="Force?",
        finding="F=ma",
        confidence=0.9,
        tags=["physics", "mechanics"],
    )
    assert "physics" in entry.tags
