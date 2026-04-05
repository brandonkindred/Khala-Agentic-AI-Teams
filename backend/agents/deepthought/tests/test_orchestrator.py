"""Tests for DeepthoughtOrchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

from deepthought.models import DecompositionStrategy, DeepthoughtRequest
from deepthought.orchestrator import DeepthoughtOrchestrator
from deepthought.result_cache import ResultCache


def _make_orchestrator(mock_llm, budget=50, cache=None):
    return DeepthoughtOrchestrator(
        llm=mock_llm, agent_budget=budget, result_cache=cache or ResultCache()
    )


def test_simple_direct_answer():
    """Orchestrator handles a simple question that needs no decomposition."""
    llm = MagicMock()
    # Strategy classification call
    llm.complete.side_effect = ['{"strategy": "none", "reasoning": "simple"}']
    llm.complete_json.return_value = {
        "summary": "Simple question",
        "can_answer_directly": True,
        "direct_answer": "The answer is 42.",
        "confidence": 0.95,
        "skill_requirements": [],
    }

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(message="What is 6 times 7?")
    resp = orch.process_message(req)

    assert "42" in resp.answer
    assert resp.total_agents_spawned == 1
    assert resp.max_depth_reached == 0
    assert not resp.agent_tree.was_decomposed
    # Should have knowledge entries
    assert len(resp.knowledge_entries) >= 1
    # Should have events
    assert len(resp.events) >= 1


def test_one_level_decomposition_with_deliberation():
    """Orchestrator decomposes, deliberates, and synthesises."""
    llm = MagicMock()
    llm.complete_json.side_effect = [
        # Root analysis
        {
            "summary": "Multi-part question",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": [
                {
                    "name": "expert_a",
                    "description": "Expert A",
                    "focus_question": "Part A?",
                    "reasoning": "Covers first aspect",
                },
            ],
        },
        # Child analysis: direct answer
        {
            "summary": "Part A",
            "can_answer_directly": True,
            "direct_answer": "A says yes",
            "confidence": 0.9,
            "skill_requirements": [],
        },
    ]
    # strategy classification, then deliberation, then synthesis
    llm.complete.side_effect = [
        '{"strategy": "by_discipline", "reasoning": "factual"}',
        "Deliberation: no contradictions, all good",
        "Synthesised: A says yes",
    ]

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(message="Complex question")
    resp = orch.process_message(req)

    assert resp.total_agents_spawned == 2
    assert resp.max_depth_reached == 1
    assert resp.agent_tree.was_decomposed
    assert resp.agent_tree.deliberation_notes is not None
    assert "Specialists consulted" in resp.answer


def test_agent_budget_limits_spawning():
    """Orchestrator stops spawning when budget is reached."""
    llm = MagicMock()
    llm.complete_json.side_effect = [
        {
            "summary": "Big question",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": [
                {
                    "name": f"expert_{i}",
                    "description": f"Expert {i}",
                    "focus_question": f"Part {i}?",
                    "reasoning": "needed",
                }
                for i in range(3)
            ],
        },
        {
            "summary": "Part 0",
            "can_answer_directly": True,
            "direct_answer": "Answer 0",
            "confidence": 0.8,
            "skill_requirements": [],
        },
        {
            "summary": "Part 1",
            "can_answer_directly": True,
            "direct_answer": "Answer 1",
            "confidence": 0.8,
            "skill_requirements": [],
        },
        {
            "summary": "Part 2",
            "can_answer_directly": True,
            "direct_answer": "Answer 2",
            "confidence": 0.8,
            "skill_requirements": [],
        },
    ]
    llm.complete.side_effect = [
        '{"strategy": "auto", "reasoning": "general"}',
        "deliberation",
        "Synthesised with budget limits",
    ]

    orch = _make_orchestrator(llm, budget=2)
    req = DeepthoughtRequest(message="Big question")
    resp = orch.process_message(req)

    assert resp.total_agents_spawned == 2
    budget_exceeded = [
        c for c in resp.agent_tree.child_results if "budget exceeded" in c.answer.lower()
    ]
    assert len(budget_exceeded) >= 1
    # Budget warning events should exist
    budget_events = [e for e in resp.events if e.event_type.value == "budget_warning"]
    assert len(budget_events) >= 1


def test_max_depth_tracking():
    """Orchestrator correctly tracks the maximum depth reached."""
    llm = MagicMock()
    llm.complete_json.side_effect = [
        # Root
        {
            "summary": "Level 0",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": [
                {
                    "name": "mid_expert",
                    "description": "Mid-level",
                    "focus_question": "Mid question?",
                    "reasoning": "needed",
                }
            ],
        },
        # Child at depth 1
        {
            "summary": "Level 1",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": [
                {
                    "name": "deep_expert",
                    "description": "Deep",
                    "focus_question": "Deep question?",
                    "reasoning": "needed",
                }
            ],
        },
        # Grandchild at depth 2: direct
        {
            "summary": "Level 2",
            "can_answer_directly": True,
            "direct_answer": "Deep answer",
            "confidence": 0.85,
            "skill_requirements": [],
        },
    ]
    llm.complete.side_effect = [
        '{"strategy": "auto", "reasoning": "complex"}',
        "deliberation depth 1",  # depth-1 deliberation (skipped, <2 children)
        "Mid synthesis",  # depth 1 synthesis
        "deliberation depth 0",
        "Root synthesis",  # depth 0 synthesis
    ]

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(message="Deep question", max_depth=10)
    resp = orch.process_message(req)

    assert resp.max_depth_reached == 2
    assert resp.total_agents_spawned == 3


def test_explicit_strategy_skips_classification():
    """When strategy is explicitly set, no classification LLM call is made."""
    llm = MagicMock()
    llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "A",
        "confidence": 0.9,
        "skill_requirements": [],
    }
    # complete should NOT be called for classification
    llm.complete.side_effect = RuntimeError("Should not be called for classification")

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(
        message="Test",
        decomposition_strategy=DecompositionStrategy.BY_CONCERN,
    )
    resp = orch.process_message(req)

    assert "A" in resp.answer


def test_conversation_history_passed_through():
    """Conversation history from the request reaches the agent."""
    llm = MagicMock()
    llm.complete.return_value = '{"strategy": "none", "reasoning": "simple"}'
    llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "Follow-up answer",
        "confidence": 0.9,
        "skill_requirements": [],
    }

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(
        message="Follow up on Mars",
        conversation_history=[
            {"role": "user", "content": "Tell me about Mars"},
            {"role": "assistant", "content": "Mars is the 4th planet."},
        ],
    )
    orch.process_message(req)

    # The analysis prompt should contain conversation history
    call_args = llm.complete_json.call_args_list[0]
    user_prompt = call_args.args[0]
    assert "Mars" in user_prompt


def test_knowledge_entries_in_response():
    """Response includes knowledge base entries from all agents."""
    llm = MagicMock()
    llm.complete.return_value = '{"strategy": "none", "reasoning": "simple"}'
    llm.complete_json.return_value = {
        "summary": "Q",
        "can_answer_directly": True,
        "direct_answer": "Knowledge answer",
        "confidence": 0.9,
        "skill_requirements": [],
    }

    orch = _make_orchestrator(llm)
    resp = orch.process_message(DeepthoughtRequest(message="Q"))

    assert len(resp.knowledge_entries) >= 1
    assert resp.knowledge_entries[0].finding.startswith("Knowledge answer")


def test_specialists_footer_format():
    """The answer includes a specialists-consulted footer when decomposed."""
    llm = MagicMock()
    llm.complete_json.side_effect = [
        {
            "summary": "Q",
            "can_answer_directly": False,
            "direct_answer": None,
            "confidence": 0.0,
            "skill_requirements": [
                {
                    "name": "physics_expert",
                    "description": "Physicist",
                    "focus_question": "Physics angle?",
                    "reasoning": "need physics",
                }
            ],
        },
        {
            "summary": "Physics",
            "can_answer_directly": True,
            "direct_answer": "F=ma",
            "confidence": 0.9,
            "skill_requirements": [],
        },
    ]
    llm.complete.side_effect = [
        '{"strategy": "by_discipline", "reasoning": "physics"}',
        "deliberation",
        "Force equals mass times acceleration.",
    ]

    orch = _make_orchestrator(llm)
    resp = orch.process_message(DeepthoughtRequest(message="Explain force"))

    assert "Specialists consulted" in resp.answer
    assert "physics_expert" in resp.answer
