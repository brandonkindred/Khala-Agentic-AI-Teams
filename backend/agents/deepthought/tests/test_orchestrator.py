"""Tests for DeepthoughtOrchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

from deepthought.models import DeepthoughtRequest
from deepthought.orchestrator import DeepthoughtOrchestrator


def _make_orchestrator(mock_llm, budget=50):
    return DeepthoughtOrchestrator(llm=mock_llm, agent_budget=budget)


def test_simple_direct_answer():
    """Orchestrator handles a simple question that needs no decomposition."""
    llm = MagicMock()
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

    assert resp.answer == "The answer is 42."
    assert resp.total_agents_spawned == 1  # root only
    assert resp.max_depth_reached == 0
    assert not resp.agent_tree.was_decomposed


def test_one_level_decomposition():
    """Orchestrator decomposes one level and synthesises."""
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
    llm.complete.return_value = "Synthesised: A says yes"

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(message="Complex question")
    resp = orch.process_message(req)

    assert resp.total_agents_spawned == 2  # root + 1 child
    assert resp.max_depth_reached == 1
    assert resp.agent_tree.was_decomposed
    assert len(resp.agent_tree.child_results) == 1
    # Answer should include specialists footer
    assert "Specialists consulted" in resp.answer


def test_agent_budget_limits_spawning():
    """Orchestrator stops spawning when budget is reached."""
    llm = MagicMock()
    # Root analysis: wants 3 children
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
        # Child 0 direct
        {
            "summary": "Part 0",
            "can_answer_directly": True,
            "direct_answer": "Answer 0",
            "confidence": 0.8,
            "skill_requirements": [],
        },
        # Child 1 direct (won't reach due to budget=2)
        {
            "summary": "Part 1",
            "can_answer_directly": True,
            "direct_answer": "Answer 1",
            "confidence": 0.8,
            "skill_requirements": [],
        },
        # Child 2 direct (won't reach due to budget=2)
        {
            "summary": "Part 2",
            "can_answer_directly": True,
            "direct_answer": "Answer 2",
            "confidence": 0.8,
            "skill_requirements": [],
        },
    ]
    llm.complete.return_value = "Synthesised with budget limits"

    # Budget of 2: root + 1 child, then budget exhausted
    orch = _make_orchestrator(llm, budget=2)
    req = DeepthoughtRequest(message="Big question")
    resp = orch.process_message(req)

    assert resp.total_agents_spawned == 2
    # Some children should have budget-exceeded messages
    budget_exceeded = [
        c for c in resp.agent_tree.child_results if "budget exceeded" in c.answer.lower()
    ]
    assert len(budget_exceeded) >= 1


def test_max_depth_tracking():
    """Orchestrator correctly tracks the maximum depth reached."""
    llm = MagicMock()
    # Root decomposes, child decomposes, grandchild answers directly
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
        "Mid synthesis",  # depth 1 synthesis
        "Root synthesis",  # depth 0 synthesis
    ]

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(message="Deep question", max_depth=10)
    resp = orch.process_message(req)

    assert resp.max_depth_reached == 2
    assert resp.total_agents_spawned == 3


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
    llm.complete.return_value = "Force equals mass times acceleration."

    orch = _make_orchestrator(llm)
    resp = orch.process_message(DeepthoughtRequest(message="Explain force"))

    assert "Specialists consulted" in resp.answer
    assert "physics_expert" in resp.answer
    assert "Physics angle?" in resp.answer
