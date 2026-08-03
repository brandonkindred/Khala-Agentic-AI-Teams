"""Tests for DeepthoughtAgent — recursive specialist node."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from deepthought.agent import MAX_CHILDREN_PER_AGENT, DeepthoughtAgent
from deepthought.knowledge_base import SharedKnowledgeBase
from deepthought.models import AgentEvent, AgentSpec
from deepthought.result_cache import ResultCache
from llm_service import LLMError


def _complete_side_effect(*deliberation_and_synthesis_values):
    """Route ``mock_llm.complete`` calls by objective.

    ``_analyse`` now issues a think=True reasoning-pass ``.complete()`` call
    before its ``.complete_json()`` formatting call; that reasoning call is
    the only one resolved by objective — it gets a generic placeholder (its
    content doesn't matter to these tests) so it doesn't consume slots meant
    for deliberation/synthesis. All other calls are consumed positionally, in
    order, from ``deliberation_and_synthesis_values``; this assumes no
    interleaving non-reasoning ``.complete()`` calls from concurrently
    scheduled child-agent threads.
    """
    it = iter(deliberation_and_synthesis_values)

    def _side_effect(*args, **kwargs):
        if kwargs.get("objective", "").startswith("analyze specialist question"):
            return "Reasoning: proceeding as configured by the test fixture."
        return next(it)

    return _side_effect


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
    # The reasoning pass that now precedes formatting also ran.
    mock_llm.complete.assert_called_once()
    assert mock_llm.complete.call_args.kwargs.get("objective", "").startswith(
        "analyze specialist question"
    )
    # Previous single-call temperature=0.3 is now split: reasoning keeps 0.3,
    # formatting uses 0.0 for deterministic transcription.
    assert mock_llm.complete.call_args.kwargs.get("temperature") == 0.3
    assert mock_llm.complete_json.call_args.kwargs.get("temperature") == 0.0


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
    # Both the reasoning pass and the formatting pass ran exactly once.
    assert mock_llm.complete.call_count == 1
    assert mock_llm.complete_json.call_count == 1


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
    # The reasoning pass (routed by objective) gets a placeholder distinct
    # from the actual forced-direct-answer call, so the two can't be
    # conflated by sharing one return value.
    mock_llm.complete.side_effect = _complete_side_effect("Forced direct answer")

    agent = _make_agent(spec, mock_llm)
    result = agent.execute(max_depth=5)

    assert not result.was_decomposed
    assert result.answer == "Forced direct answer"
    assert result.child_results == []
    assert mock_llm.complete.call_count == 2
    assert mock_llm.complete_json.call_count == 1


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
    # First non-reasoning complete call = deliberation, second = synthesis
    mock_llm.complete.side_effect = _complete_side_effect(
        '{"contradictions": [], "gaps": [], "agreements": ["Both say 42"], '
        '"quality_flags": [], "synthesis_guidance": "Straightforward agreement"}',
        "Synthesised: both say 42",
    )

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
    # Two-pass analysis on root + both children: three formatting calls and
    # three reasoning completes (objective-tagged), plus deliberation and
    # synthesis completes.
    assert mock_llm.complete_json.call_count == 3
    reasoning_calls = [
        c
        for c in mock_llm.complete.call_args_list
        if str(c.kwargs.get("objective", "")).startswith("analyze specialist question")
    ]
    assert len(reasoning_calls) == 3
    assert mock_llm.complete.call_count == 5  # 3 reasoning + deliberation + synthesis


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
    mock_llm.complete.assert_not_called()
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
    mock_llm.complete.assert_not_called()
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
    assert len(events) >= 2
    # Should have at least ANALYSING and COMPLETE
    from deepthought.models import AgentEventType

    assert AgentEventType.AGENT_ANALYSING in event_types
    assert AgentEventType.AGENT_COMPLETE in event_types

    # Schema-contract check: pin the fields SSE consumers rely on, so a
    # rename/removal fails here instead of silently breaking streaming
    # clients. (Unlike `assert AgentEvent.model_fields`, which is always
    # truthy for any model with fields, this fails if a specific field goes
    # missing.)
    assert {"event_type", "agent_id", "agent_name", "depth"} <= AgentEvent.model_fields.keys()

    # Value-level check: every emitted event actually describes this root
    # agent's execution, not just the right count/types of events.
    for event in events:
        assert event.agent_id == root_spec.agent_id
        assert event.agent_name == root_spec.name
        assert event.depth == root_spec.depth


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
    mock_llm.complete.side_effect = _complete_side_effect("deliberation", "synthesis")

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

    # Verify the original_query appears in the analysis reasoning-pass system
    # prompt (the root's analyse call runs synchronously before any children
    # are spawned, so it's the first .complete() call recorded).
    first_complete_call = mock_llm.complete.call_args_list[0]
    system_prompt = first_complete_call.kwargs.get("system_prompt", "")
    assert original_msg in system_prompt

    # Every formatting call (root analysis and child analysis both make one)
    # sees only its own reasoning pass's prose, never the raw original_query
    # text.
    for format_call in mock_llm.complete_json.call_args_list:
        format_input = (format_call.kwargs.get("system_prompt") or "") + str(
            format_call.args[0] if format_call.args else ""
        )
        assert original_msg not in format_input


# ------------------------------------------------------------------
# Conversation history threading
# ------------------------------------------------------------------


def test_conversation_history_in_prompt(root_spec, mock_llm):
    """Conversation history is included in the analysis prompt."""
    history = [
        {"role": "user", "content": "Tell me about Mars"},
        {"role": "assistant", "content": "Mars is the 4th planet."},
    ]
    mock_llm.complete.return_value = (
        "The user previously asked about Mars; answering the follow-up directly."
    )
    mock_llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "Follow-up answer",
        "confidence": 0.9,
        "skill_requirements": [],
    }

    agent = _make_agent(root_spec, mock_llm, conversation_history=history)
    agent.execute(max_depth=10)

    # The user prompt should contain the conversation history. It's now the
    # reasoning-pass .complete() call that carries the user-facing prompt;
    # the .complete_json() formatting call only sees the reasoning prose.
    first_complete_call = mock_llm.complete.call_args_list[0]
    user_prompt = first_complete_call.args[0] if first_complete_call.args else ""
    assert "Mars" in user_prompt

    # The formatting call sees only the reasoning pass's prose, never the
    # raw conversation history — check for the raw history's structure
    # (role labels, the verbatim assistant reply), not the topic word
    # "Mars", since the reasoning prose legitimately mentions the topic too.
    format_call = mock_llm.complete_json.call_args
    format_input = (format_call.kwargs.get("system_prompt") or "") + str(
        format_call.args[0] if format_call.args else ""
    )
    assert "Mars is the 4th planet." not in format_input
    assert "User: Tell me about Mars" not in format_input
    assert "Assistant: Mars is the 4th planet." not in format_input
    # Positive check: the reasoning pass's prose (not the raw history) is
    # what actually reaches the formatting call.
    assert mock_llm.complete.return_value in format_input


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
    # "deliberation notes" is the reasoning-pass placeholder consumed by the
    # single deliberation .complete() call; "Synthesised" is consumed by the
    # single non-reasoning synthesis .complete() call after decomposition.
    mock_llm.complete.side_effect = _complete_side_effect("deliberation notes", "Synthesised")

    spawned = []

    def track_spawn(spec):
        spawned.append(spec)
        return True

    agent = _make_agent(root_spec, mock_llm, on_spawned=track_spawn)
    result = agent.execute(max_depth=10)

    assert result.was_decomposed
    assert len(result.child_results) == MAX_CHILDREN_PER_AGENT
    assert len(spawned) == MAX_CHILDREN_PER_AGENT
    assert result.answer == "Synthesised"


# ------------------------------------------------------------------
# Fallback on LLM error
# ------------------------------------------------------------------


@pytest.mark.parametrize("failing_call", ["reasoning", "formatting"])
def test_analysis_llm_error_fallback(root_spec, mock_llm, failing_call):
    """If either analysis LLM call raises LLMError, agent falls back to a direct answer."""
    if failing_call == "reasoning":
        mock_llm.complete.side_effect = LLMError("LLM unavailable")
    else:
        mock_llm.complete_json.side_effect = LLMError("LLM unavailable")
        # Distinct values for the analysis reasoning call vs. the
        # _force_direct_answer fallback's own .complete() call, so the
        # assertion below can only pass if the fallback call actually ran —
        # a shared .return_value would let echoed reasoning prose masquerade
        # as the fallback answer.
        mock_llm.complete.side_effect = ["Reasoning prose (not the answer)", "Fallback answer"]

    agent = _make_agent(root_spec, mock_llm)
    result = agent.execute(max_depth=10)

    assert not result.was_decomposed
    if failing_call == "formatting":
        # The formatting call fails, but the reasoning call's answer is
        # still recoverable via _force_direct_answer's own .complete() call.
        assert result.answer == "Fallback answer"
    else:
        # Both the analysis reasoning call and the force-direct fallback's
        # own .complete() call fail, so the agent falls back to its final,
        # LLM-free placeholder.
        assert result.answer == f"Unable to provide analysis for: {root_spec.focus_question}"


def test_analysis_non_llm_error_propagates(root_spec, mock_llm):
    """A non-LLMError failure (a programming bug) is not swallowed as a fallback.

    ``_analyse`` only catches ``LLMError`` — the LLM layer's own failure
    hierarchy — so a defect surfacing as e.g. ``TypeError`` must propagate
    instead of masquerading as a low-confidence fallback ``QueryAnalysis``.
    """
    mock_llm.complete_json.side_effect = TypeError("malformed response shape")

    agent = _make_agent(root_spec, mock_llm)

    with pytest.raises(TypeError, match="malformed response shape"):
        agent.execute(max_depth=10)


# ------------------------------------------------------------------
# Knowledge base population
# ------------------------------------------------------------------


def test_findings_stored_in_knowledge_base(root_spec, mock_llm, knowledge_base):
    """After answering, the agent stores its finding in the knowledge base."""
    mock_llm.complete.return_value = (
        "The user asked a direct question that can be answered from general knowledge."
    )
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
