"""Tests for DeepthoughtOrchestrator."""

from __future__ import annotations

from unittest.mock import MagicMock

from deepthought.models import DecompositionStrategy, DeepthoughtRequest
from deepthought.orchestrator import DeepthoughtOrchestrator
from deepthought.result_cache import ResultCache
from llm_service.interface import LLMClient


def _make_orchestrator(mock_llm, budget=50, cache=None):
    return DeepthoughtOrchestrator(
        llm=mock_llm, agent_budget=budget, result_cache=cache or ResultCache()
    )


def _reasoning_stub(*args, **kwargs):
    """Generic placeholder for _analyse's think=True reasoning-pass .complete() call.

    Content doesn't matter to these tests — only the downstream .complete_json()
    formatting call (mocked separately) determines the analysis outcome.
    """
    return "Reasoning: proceeding as configured by the test fixture."


def _complete_side_effect(*classification_values):
    """Route ``mock_llm.complete`` calls by objective.

    ``_analyse`` issues a think=True reasoning-pass ``.complete()`` call
    (objective "analyze specialist question (reasoning)") independent of
    strategy classification (objective "classify question strategy") —
    resolve by objective, not call position, so both can be exercised (or
    the reasoning pass alone, when classification is skipped) without
    tripping over each other.
    """
    it = iter(classification_values)

    def _side_effect(*args, **kwargs):
        if kwargs.get("objective", "").startswith("analyze specialist question"):
            return _reasoning_stub(*args, **kwargs)
        return next(it)

    return _side_effect


def test_simple_direct_answer():
    """Orchestrator handles a simple question that needs no decomposition."""
    llm = MagicMock()
    # Strategy classification call
    llm.complete.side_effect = _complete_side_effect('{"strategy": "none", "reasoning": "simple"}')
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


def test_one_level_decomposition_skips_deliberation_for_single_child():
    """Orchestrator decomposes and synthesises, skipping deliberation (1 child < 2 threshold)."""
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
    # strategy classification, then synthesis — deliberation is skipped with a
    # single child (< 2), so it never calls .complete(). Reasoning-pass
    # .complete() calls (root + child analyse) are routed separately by objective.
    llm.complete.side_effect = _complete_side_effect(
        '{"strategy": "by_discipline", "reasoning": "factual"}',
        "Synthesised: A says yes",
    )

    orch = _make_orchestrator(llm)
    req = DeepthoughtRequest(message="Complex question")
    resp = orch.process_message(req)

    assert resp.total_agents_spawned == 2
    assert resp.max_depth_reached == 1
    assert resp.agent_tree.was_decomposed
    assert resp.agent_tree.deliberation_notes == ""
    assert resp.answer.startswith("Synthesised: A says yes")
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
    llm.complete.side_effect = _complete_side_effect(
        '{"strategy": "auto", "reasoning": "general"}',
        "deliberation",
        "Synthesised with budget limits",
    )

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
    # Only 3 non-reasoning .complete() calls actually happen: strategy
    # classification, then one synthesis call per level (depth 1, then depth
    # 0) — deliberation is skipped at both depths since each node has a
    # single child (below the >=2-children threshold), so it never draws
    # from this queue. Supplying values for the skipped deliberation calls
    # would go unconsumed and silently shift the labels on the values that
    # ARE consumed onto the wrong calls.
    llm.complete.side_effect = _complete_side_effect(
        '{"strategy": "auto", "reasoning": "complex"}',
        "Mid synthesis",  # depth 1 synthesis
        "Root synthesis",  # depth 0 synthesis
    )

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

    # complete should NOT be called for classification (it's still legitimately
    # called for _analyse's think=True reasoning pass, which _complete_side_effect
    # handles separately).
    def _no_classification_calls(*args, **kwargs):
        if not kwargs.get("objective", "").startswith("analyze specialist question"):
            raise RuntimeError("Should not be called for classification")
        return _reasoning_stub(*args, **kwargs)

    llm.complete.side_effect = _no_classification_calls

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
    # Route by objective so the strategy-classification call and _analyse's
    # think=True reasoning-pass call don't share a return value (the latter
    # expects prose, not the classification JSON string) — same conflation
    # this helper exists to avoid elsewhere in this file.
    llm.complete.side_effect = _complete_side_effect('{"strategy": "none", "reasoning": "simple"}')
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

    # The analysis prompt should contain conversation history. It's now the
    # reasoning-pass .complete() call that carries the user-facing prompt.
    analyse_calls = [
        c
        for c in llm.complete.call_args_list
        if c.kwargs.get("objective", "").startswith("analyze specialist question")
    ]
    assert analyse_calls, "expected at least one reasoning-pass call"
    user_prompt = analyse_calls[0].args[0]
    assert "Mars" in user_prompt


def test_knowledge_entries_in_response():
    """Response includes knowledge base entries from all agents."""
    llm = MagicMock()
    # Route by objective so the strategy-classification call and _analyse's
    # think=True reasoning-pass call don't share a return value (the latter
    # expects prose, not the classification JSON string).
    llm.complete.side_effect = _complete_side_effect('{"strategy": "none", "reasoning": "simple"}')
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
    # Single child → deliberation is skipped (< 2 children), so only
    # classification and synthesis call .complete() non-reasoning.
    llm.complete.side_effect = _complete_side_effect(
        '{"strategy": "by_discipline", "reasoning": "physics"}',
        "Force equals mass times acceleration.",
    )

    orch = _make_orchestrator(llm)
    resp = orch.process_message(DeepthoughtRequest(message="Explain force"))

    assert resp.answer.startswith("Force equals mass times acceleration.")
    assert "Specialists consulted" in resp.answer
    assert "physics_expert" in resp.answer


def test_budget_warning_flows_through_collect_event():
    """_register_spawn routes budget-exhausted vetoes through _collect_event
    (so SSE streams see them) and does not deadlock on the non-reentrant lock."""
    from deepthought.models import AgentEventType, AgentSpec

    orch = _make_orchestrator(MagicMock(), budget=1)

    # Capture every event that _collect_event sees.
    captured = []
    original_collect = orch._collect_event

    def spy(event):
        captured.append(event)
        original_collect(event)

    orch._collect_event = spy  # type: ignore[assignment]

    # First spawn consumes the budget.
    spec1 = AgentSpec(
        agent_id="a1",
        name="agent_one",
        role_description="first",
        focus_question="Q?",
        depth=0,
        parent_id=None,
    )
    assert orch._register_spawn(spec1) is True

    # Second spawn is vetoed; must emit a BUDGET_WARNING through _collect_event.
    spec2 = AgentSpec(
        agent_id="a2",
        name="agent_two",
        role_description="second",
        focus_question="Q?",
        depth=1,
        parent_id="a1",
    )
    assert orch._register_spawn(spec2) is False

    budget_events = [e for e in captured if e.event_type == AgentEventType.BUDGET_WARNING]
    assert len(budget_events) == 1
    assert budget_events[0].agent_id == "a2"
    # And it made it into the stored events list as well.
    assert any(e.event_type == AgentEventType.BUDGET_WARNING for e in orch._events)


def test_default_llm_exposes_complete_and_complete_json(monkeypatch):
    """Regression guard: the default (no ``llm=`` passed) client is a real ``LLMClient``.

    A ``strands.Agent`` wrapping ``get_strands_model`` does NOT expose
    ``complete``/``complete_json`` (its public surface is ``__call__``) — every
    real completion would silently raise ``AttributeError``, swallowed by the
    broad ``except Exception`` in ``DeepthoughtAgent``'s LLM methods, and fall
    through to hard-coded fallback text. ``LLM_PROVIDER=dummy`` exercises the
    real (unmocked) default-construction branch without touching Postgres.
    """
    monkeypatch.setenv("LLM_PROVIDER", "dummy")
    orch = DeepthoughtOrchestrator()
    assert isinstance(orch._llm, LLMClient)
    assert callable(orch._llm.complete)
    assert callable(orch._llm.complete_json)
    # The dummy client must actually answer, not raise.
    assert isinstance(orch._llm.complete("hello", objective="test"), str)
    assert isinstance(orch._llm.complete_json("hello", objective="test"), dict)
