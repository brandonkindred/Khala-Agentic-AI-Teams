"""Tests for DeepthoughtAgent — recursive specialist node."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from deepthought.agent import MAX_CHILDREN_PER_AGENT, DeepthoughtAgent
from deepthought.models import AgentSpec


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


def _make_agent(spec, llm, on_spawned=None):
    return DeepthoughtAgent(spec=spec, llm=llm, on_agent_spawned=on_spawned)


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
    assert result.confidence == 0.95
    assert result.child_results == []
    mock_llm.complete_json.assert_called_once()


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
    # LLM analysis says decompose, but depth is at max
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
# Decomposition path
# ------------------------------------------------------------------


def test_single_level_decomposition(root_spec, mock_llm):
    """Agent decomposes into children, each answers directly, then synthesises."""
    # Root analysis: needs 2 specialists
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
    mock_llm.complete.return_value = "Synthesised: both say 42"

    spawned = []

    def track_spawn(spec):
        spawned.append(spec)
        return True

    agent = _make_agent(root_spec, mock_llm, on_spawned=track_spawn)
    result = agent.execute(max_depth=10)

    assert result.was_decomposed
    assert len(result.child_results) == 2
    assert result.answer == "Synthesised: both say 42"
    assert len(spawned) == 2


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
    mock_llm.complete.return_value = "Synthesised from truncated"

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

    # Root returns 8 skills
    analysis_responses = [
        {
            "summary": "Big question",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": skills,
        }
    ]
    # Each child answers directly
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
    mock_llm.complete.return_value = "Synthesised"

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
