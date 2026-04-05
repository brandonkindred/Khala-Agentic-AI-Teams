"""Tests for DeepthoughtAgent — recursive specialist node."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from deepthought.agent import MAX_CHILDREN_PER_AGENT, DeepthoughtAgent
from deepthought.knowledge_base import SharedKnowledgeBase
from deepthought.models import AgentEvent, AgentSpec
from deepthought.result_cache import ResultCache


@pytest.fixture()
def root_spec():
    return AgentSpec(
        agent_id="root-1",
        name="general_analyst",
        role_description="General analyst",
        focus_question="What is the meaning of life?",
        depth=0,
        parent_id=None,
    )


@pytest.fixture()
def mock_llm():
    return MagicMock()


@pytest.fixture()
def knowledge_base():
    return SharedKnowledgeBase()


def _make_agent(spec, llm, on_spawned=None, kb=None, cache=None, on_event=None, **kwargs):
    return DeepthoughtAgent(
        spec=spec,
        llm=llm,
        knowledge_base=kb or SharedKnowledgeBase(),
        result_cache=cache,
        on_agent_spawned=on_spawned,
        on_event=on_event,
        **kwargs,
    )


# ------------------------------------------------------------------
# Direct answer path
# ------------------------------------------------------------------


def test_direct_answer(root_spec, mock_llm):
    """When the LLM says can_answer_directly=True, no children are spawned."""
    mock_llm.complete_json.return_value = {
        "summary": "Meaning of life",
        "can_answer_directly": True,
        "direct_answer": "42",
        "confidence": 0.95,
        "skill_requirements": [],
    }

    agent = _make_agent(root_spec, mock_llm)
    result = agent.execute(max_depth=10)

    assert not result.was_decomposed
    assert result.answer == "42"
    assert result.child_results == []
    mock_llm.complete_json.assert_called_once()


# ------------------------------------------------------------------
# Structural confidence
# ------------------------------------------------------------------


def test_structural_confidence_direct(root_spec, mock_llm):
    """Direct answers use blended structural confidence, not raw self-assessment."""
    mock_llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "Answer",
        "confidence": 0.9,
        "skill_requirements": [],
    }

    agent = _make_agent(root_spec, mock_llm)
    result = agent.execute(max_depth=10)

    # 0.4 + 0.6 * min(0.9, 0.95) = 0.4 + 0.54 = 0.94
    assert result.confidence == 0.94


# ------------------------------------------------------------------
# Depth limit enforcement
# ------------------------------------------------------------------


def test_depth_limit_forces_direct(mock_llm):
    """At max depth, agent must answer directly even if analysis wants to decompose."""
    spec = AgentSpec(
        agent_id="deep-1",
        name="deep_agent",
        role_description="Deep specialist",
        focus_question="Sub-question?",
        depth=5,
        parent_id="parent-1",
    )
    mock_llm.complete_json.return_value = {
        "summary": "Sub-question",
        "can_answer_directly": False,
        "direct_answer": None,
        "confidence": 0.0,
        "skill_requirements": [
            {
                "name": "sub_expert",
                "description": "Sub-expert",
                "focus_question": "More detail?",
                "reasoning": "needed",
            }
        ],
    }
    mock_llm.complete.return_value = "Forced direct answer"

    agent = _make_agent(spec, mock_llm)
    result = agent.execute(max_depth=5)

    assert not result.was_decomposed
    assert result.answer == "Forced direct answer"
    assert result.child_results == []


# ------------------------------------------------------------------
# Decomposition with deliberation
# ------------------------------------------------------------------


def test_decomposition_with_deliberation(root_spec, mock_llm):
    """Agent decomposes, deliberates, then synthesises."""
    mock_llm.complete_json.side_effect = [
        # Root analysis
        {
            "summary": "Complex question",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": [
                {
                    "name": "philosophy_expert",
                    "description": "Philosopher",
                    "focus_question": "What do philosophers say?",
                    "reasoning": "Need philosophical perspective",
                },
                {
                    "name": "science_expert",
                    "description": "Scientist",
                    "focus_question": "What does science say?",
                    "reasoning": "Need scientific perspective",
                },
            ],
        },
        # Child 1 analysis: direct answer
        {
            "summary": "Philosophy perspective",
            "can_answer_directly": True,
            "direct_answer": "Philosophers say 42",
            "confidence": 0.8,
            "skill_requirements": [],
        },
        # Child 2 analysis: direct answer
        {
            "summary": "Science perspective",
            "can_answer_directly": True,
            "direct_answer": "Science says 42",
            "confidence": 0.9,
            "skill_requirements": [],
        },
    ]
    # First complete call = deliberation, second = synthesis
    mock_llm.complete.side_effect = [
        '{"contradictions": [], "gaps": [], "agreements": ["Both say 42"], '
        '"quality_flags": [], "synthesis_guidance": "Straightforward agreement"}',
        "Synthesised: both say 42",
    ]

    spawned = []

    def track_spawn(spec):
        spawned.append(spec)
        return True

    agent = _make_agent(root_spec, mock_llm, on_spawned=track_spawn)
    result = agent.execute(max_depth=10)

    assert result.was_decomposed
    assert len(result.child_results) == 2
    assert result.answer == "Synthesised: both say 42"
    assert result.deliberation_notes is not None
    assert len(spawned) == 2


# ------------------------------------------------------------------
# Knowledge base deduplication
# ------------------------------------------------------------------


def test_knowledge_base_deduplication(mock_llm, knowledge_base):
    """When a similar question already has a finding, the agent reuses it."""
    from deepthought.models import KnowledgeEntry

    # Pre-populate knowledge base with a finding for a similar question
    knowledge_base.add(
        KnowledgeEntry(
            agent_id="prior-1",
            agent_name="prior_expert",
            focus_question="What is the meaning of life?",
            finding="The meaning is 42",
            confidence=0.9,
            tags=["meaning", "life"],
        )
    )

    spec = AgentSpec(
        agent_id="dup-1",
        name="duplicate_analyst",
        role_description="Analyst",
        focus_question="What is the meaning of life?",
        depth=1,  # depth > 0 enables dedup
        parent_id="root-1",
    )

    agent = _make_agent(spec, mock_llm, kb=knowledge_base)
    result = agent.execute(max_depth=10)

    assert result.reused_from_cache
    assert result.answer == "The meaning is 42"
    # LLM should not have been called
    mock_llm.complete_json.assert_not_called()


# ------------------------------------------------------------------
# Result cache
# ------------------------------------------------------------------


def test_result_cache_hit(mock_llm):
    """Cached results are returned without LLM calls."""
    from deepthought.models import AgentResult

    cache = ResultCache()
    cached_result = AgentResult(
        agent_id="old-1",
        agent_name="old_agent",
        depth=0,
        focus_question="cached question",
        answer="cached answer",
        confidence=0.85,
    )
    cache.put("cached question", cached_result)

    spec = AgentSpec(
        agent_id="new-1",
        name="new_agent",
        role_description="Agent",
        focus_question="cached question",
        depth=0,
        parent_id=None,
    )

    agent = _make_agent(spec, mock_llm, cache=cache)
    result = agent.execute(max_depth=10)

    assert result.reused_from_cache
    assert result.answer == "cached answer"
    assert result.agent_id == "new-1"  # ID should be updated
    mock_llm.complete_json.assert_not_called()


# ------------------------------------------------------------------
# Event emission
# ------------------------------------------------------------------


def test_events_emitted(root_spec, mock_llm):
    """Agent emits events during execution."""
    mock_llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "A",
        "confidence": 0.9,
        "skill_requirements": [],
    }

    events: list[AgentEvent] = []

    agent = _make_agent(root_spec, mock_llm, on_event=events.append)
    agent.execute(max_depth=10)

    event_types = [e.event_type for e in events]
    assert AgentEvent.model_fields  # sanity check
    assert len(events) >= 2
    # Should have at least ANALYSING and COMPLETE
    from deepthought.models import AgentEventType

    assert AgentEventType.AGENT_ANALYSING in event_types
    assert AgentEventType.AGENT_COMPLETE in event_types


# ------------------------------------------------------------------
# Original query threading
# ------------------------------------------------------------------


def test_original_query_threaded_to_children(root_spec, mock_llm):
    """Children receive the original_query from the root."""
    original_msg = "Top-level user question about everything"

    mock_llm.complete_json.side_effect = [
        {
            "summary": "Big Q",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": [
                {
                    "name": "child_expert",
                    "description": "Child",
                    "focus_question": "Sub Q?",
                    "reasoning": "needed",
                }
            ],
        },
        {
            "summary": "Sub Q",
            "can_answer_directly": True,
            "direct_answer": "Sub answer",
            "confidence": 0.8,
            "skill_requirements": [],
        },
    ]
    mock_llm.complete.side_effect = [
        "deliberation",
        "synthesis",
    ]

    spawned_agents = []

    def track(spec):
        spawned_agents.append(spec)
        return True

    agent = _make_agent(
        root_spec,
        mock_llm,
        on_spawned=track,
        original_query=original_msg,
    )
    agent.execute(max_depth=10)

    # Verify the original_query appears in the analysis system prompt
    # by checking the LLM calls
    first_call_kwargs = mock_llm.complete_json.call_args_list[0]
    system_prompt = first_call_kwargs.kwargs.get("system_prompt", "")
    assert original_msg in system_prompt


# ------------------------------------------------------------------
# Conversation history threading
# ------------------------------------------------------------------


def test_conversation_history_in_prompt(root_spec, mock_llm):
    """Conversation history is included in the analysis prompt."""
    history = [
        {"role": "user", "content": "Tell me about Mars"},
        {"role": "assistant", "content": "Mars is the 4th planet."},
    ]
    mock_llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "Follow-up answer",
        "confidence": 0.9,
        "skill_requirements": [],
    }

    agent = _make_agent(root_spec, mock_llm, conversation_history=history)
    agent.execute(max_depth=10)

    # The user prompt should contain the conversation history
    first_call_args = mock_llm.complete_json.call_args_list[0]
    user_prompt = first_call_args.args[0] if first_call_args.args else ""
    assert "Mars" in user_prompt


# ------------------------------------------------------------------
# Budget enforcement
# ------------------------------------------------------------------


def test_budget_exceeded_vetoes_children(root_spec, mock_llm):
    """When on_agent_spawned returns False, child gets a truncation message."""
    mock_llm.complete_json.return_value = {
        "summary": "Question",
        "can_answer_directly": False,
        "direct_answer": None,
        "confidence": 0.0,
        "skill_requirements": [
            {
                "name": "expert_a",
                "description": "Expert A",
                "focus_question": "Q?",
                "reasoning": "needed",
            }
        ],
    }
    mock_llm.complete.side_effect = ["deliberation", "Synthesised from truncated"]

    def deny_spawn(_spec):
        return False

    agent = _make_agent(root_spec, mock_llm, on_spawned=deny_spawn)
    result = agent.execute(max_depth=10)

    assert result.was_decomposed
    assert len(result.child_results) == 1
    assert "budget exceeded" in result.child_results[0].answer.lower()


# ------------------------------------------------------------------
# Max children cap
# ------------------------------------------------------------------


def test_max_children_capped(root_spec, mock_llm):
    """Even if LLM returns >5 skills, only MAX_CHILDREN_PER_AGENT are used."""
    skills = [
        {
            "name": f"expert_{i}",
            "description": f"Expert {i}",
            "focus_question": f"Q{i}?",
            "reasoning": "needed",
        }
        for i in range(8)
    ]

    analysis_responses = [
        {
            "summary": "Big question",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": skills,
        }
    ]
    for i in range(MAX_CHILDREN_PER_AGENT):
        analysis_responses.append(
            {
                "summary": f"Sub {i}",
                "can_answer_directly": True,
                "direct_answer": f"Answer {i}",
                "confidence": 0.8,
                "skill_requirements": [],
            }
        )

    mock_llm.complete_json.side_effect = analysis_responses
    mock_llm.complete.side_effect = ["deliberation notes", "Synthesised"]

    spawned = []

    def track_spawn(spec):
        spawned.append(spec)
        return True

    agent = _make_agent(root_spec, mock_llm, on_spawned=track_spawn)
    result = agent.execute(max_depth=10)

    assert result.was_decomposed
    assert len(result.child_results) <= MAX_CHILDREN_PER_AGENT
    assert len(spawned) <= MAX_CHILDREN_PER_AGENT


# ------------------------------------------------------------------
# Fallback on LLM error
# ------------------------------------------------------------------


def test_analysis_llm_error_fallback(root_spec, mock_llm):
    """If the analysis LLM call raises, agent falls back to a direct answer."""
    mock_llm.complete_json.side_effect = RuntimeError("LLM unavailable")
    mock_llm.complete.return_value = "Fallback answer"

    agent = _make_agent(root_spec, mock_llm)
    result = agent.execute(max_depth=10)

    assert not result.was_decomposed
    assert result.answer == "Fallback answer"


# ------------------------------------------------------------------
# Knowledge base population
# ------------------------------------------------------------------


def test_findings_stored_in_knowledge_base(root_spec, mock_llm, knowledge_base):
    """After answering, the agent stores its finding in the knowledge base."""
    mock_llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "The answer",
        "confidence": 0.9,
        "skill_requirements": [],
    }

    agent = _make_agent(root_spec, mock_llm, kb=knowledge_base)
    agent.execute(max_depth=10)

    entries = knowledge_base.all_entries()
    assert len(entries) == 1
    assert entries[0].finding == "The answer"
    assert entries[0].agent_name == "general_analyst"
