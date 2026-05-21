"""More targeted tests for ghost_writer_agent and adjacent modules.

Covers:
* ``GhostWriterElicitationAgent._evaluate_sufficiency`` with parse failure + retry success.
* ``_generate_follow_up`` and ``_compile_narrative`` happy + error paths.
* ``_find_gaps_via_llm`` happy + error paths.
* ``_plan_to_text``.
* ``conduct_interview`` quick exits (skipped, cancelled, no-experience).
"""

from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import MagicMock


def _content_plan():
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    return ContentPlan(
        overarching_topic="Building scalable APIs",
        narrative_flow="Intro, body, wrap",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="hook", order=0),
            ContentPlanSection(title="Body", coverage_description="depth", order=1),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )


def _gap():
    from ghost_writer_agent.models import StoryGap

    return StoryGap(
        section_title="Intro",
        section_context="Hook the reader",
        seed_question="Got a story?",
    )


# ---------------------------------------------------------------------------
# _plan_to_text
# ---------------------------------------------------------------------------


def test_ghost_plan_to_text_renders_sections() -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    plan = _content_plan()
    text = GhostWriterElicitationAgent._plan_to_text(plan)
    assert "Topic/thesis: Building scalable APIs" in text
    assert "Section: Intro" in text
    assert "Coverage: hook" in text


# ---------------------------------------------------------------------------
# _evaluate_sufficiency — parse failure retry + success
# ---------------------------------------------------------------------------


def _patch_agent(monkeypatch, responses: List[Any]) -> None:
    """Stub the strands Agent class inside ghost_writer_agent.agent."""
    import ghost_writer_agent.agent as gw_agent

    state = {"i": 0}

    class _StubAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt: str) -> str:
            r = responses[min(state["i"], len(responses) - 1)]
            state["i"] += 1
            if isinstance(r, Exception):
                raise r
            return r

    monkeypatch.setattr(gw_agent, "Agent", _StubAgent)


def test_ghost_evaluate_sufficiency_success(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(
        monkeypatch,
        [
            json.dumps(
                {
                    "sufficient": True,
                    "no_experience": False,
                    "story_context": "client",
                    "missing": None,
                }
            )
        ],
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [{"role": "agent", "content": "Got a story?"}])
    assert out["sufficient"] is True
    assert out["story_context"] == "client"


def test_ghost_evaluate_sufficiency_parse_retry_succeeds(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(
        monkeypatch,
        [
            "not-json",
            json.dumps(
                {"sufficient": True, "no_experience": False, "story_context": None, "missing": None}
            ),
        ],
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out["sufficient"] is True


def test_ghost_evaluate_sufficiency_falls_back_default(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["not-json-1", "not-json-2"])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out == {
        "sufficient": False,
        "no_experience": False,
        "story_context": None,
        "missing": None,
    }


def test_ghost_evaluate_sufficiency_exception_then_default(monkeypatch) -> None:
    import ghost_writer_agent.agent as gw_agent
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("LLM exploded")

    monkeypatch.setattr(gw_agent, "Agent", _Boom)
    monkeypatch.setattr(gw_agent.time, "sleep", lambda *_: None)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out["sufficient"] is False


# ---------------------------------------------------------------------------
# _generate_follow_up
# ---------------------------------------------------------------------------


def test_ghost_generate_follow_up_happy(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["What happened next?"])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_follow_up(
        _gap(),
        [{"role": "agent", "content": "Got a story?"}, {"role": "user", "content": "yes"}],
        {"missing": "outcome", "story_context": "client"},
    )
    assert out == "What happened next?"


def test_ghost_generate_follow_up_error_returns_none(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [RuntimeError("nope")])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_follow_up(_gap(), [], {})
    assert out is None


def test_ghost_generate_follow_up_empty_response(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["   "])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._generate_follow_up(_gap(), [], {}) is None


# ---------------------------------------------------------------------------
# _compile_narrative
# ---------------------------------------------------------------------------


def test_ghost_compile_narrative_empty_user_content() -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._compile_narrative(_gap(), [{"role": "agent", "content": "hi"}])
    assert out is None


def test_ghost_compile_narrative_happy_path_with_context(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["I once shipped a feature in 24 hours."])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._compile_narrative(
        _gap(),
        [
            {"role": "agent", "content": "Tell me about it"},
            {"role": "user", "content": "Sure! I shipped a feature."},
        ],
        story_context="personal",
    )
    assert "shipped a feature" in out


def test_ghost_compile_narrative_handles_errors(monkeypatch) -> None:
    import ghost_writer_agent.agent as gw_agent
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("nope")

    monkeypatch.setattr(gw_agent, "Agent", _Boom)
    monkeypatch.setattr(gw_agent.time, "sleep", lambda *_: None)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._compile_narrative(
        _gap(),
        [{"role": "user", "content": "Story content"}],
        story_context="employer",
    )
    assert out is None


# ---------------------------------------------------------------------------
# _find_gaps_via_llm — direct path
# ---------------------------------------------------------------------------


def test_ghost_find_gaps_via_llm_success(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    plan = _content_plan()
    _patch_agent(
        monkeypatch,
        [
            json.dumps(
                [
                    {
                        "section_title": "Intro",
                        "section_context": "Lead with a story",
                        "seed_question": "Tell me a moment",
                    },
                    {
                        "section_title": "Body",
                        "section_context": "Deep dive",
                        # Empty seed_question → fallback generated
                        "seed_question": "",
                    },
                ]
            )
        ],
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._find_gaps_via_llm(plan)
    assert len(out) == 2
    assert out[0].seed_question == "Tell me a moment"
    # Second gap has a fallback question
    assert "deep dive" in out[1].seed_question.lower()


def test_ghost_find_gaps_via_llm_no_array_returns_empty(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["no brackets here"])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._find_gaps_via_llm(_content_plan())
    assert out == []


def test_ghost_find_gaps_via_llm_parse_error_retry_then_fail(monkeypatch) -> None:
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["[not-json", "[also-not-json"])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._find_gaps_via_llm(_content_plan()) == []


def test_ghost_find_gaps_via_llm_exception_then_recover(monkeypatch) -> None:
    import ghost_writer_agent.agent as gw_agent
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    state = {"i": 0}

    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            i = state["i"]
            state["i"] += 1
            if i == 0:
                raise RuntimeError("transient")
            return json.dumps(
                [
                    {
                        "section_title": "Intro",
                        "section_context": "Hook",
                        "seed_question": "Got a moment?",
                    }
                ]
            )

    monkeypatch.setattr(gw_agent, "Agent", _Stub)
    monkeypatch.setattr(gw_agent.time, "sleep", lambda *_: None)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._find_gaps_via_llm(_content_plan())
    assert len(out) == 1


def test_ghost_find_story_gaps_uses_plan_opportunities_when_present(monkeypatch) -> None:
    """find_story_gaps short-circuits to opportunities, avoiding LLM gap-finding."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    from llm_service import DummyLLMClient

    sec = ContentPlanSection(
        title="A", coverage_description="cov", order=0, story_opportunity="A bug story"
    )
    plan = ContentPlan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[sec],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    monkeypatch.setattr(agent, "_generate_friendly_seeds", lambda opps: [f"q-{o}" for o in opps])
    out = agent.find_story_gaps(plan)
    assert len(out) == 1
    assert out[0].seed_question.startswith("q-A bug story")


def test_ghost_find_story_gaps_falls_back_to_llm(monkeypatch) -> None:
    """No story_opportunity → goes through _find_gaps_via_llm."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    monkeypatch.setattr(agent, "_find_gaps_via_llm", lambda plan: ["sentinel"])
    out = agent.find_story_gaps(_content_plan())
    assert out == ["sentinel"]


# ---------------------------------------------------------------------------
# _generate_friendly_seeds — dict response forms
# ---------------------------------------------------------------------------


def test_ghost_generate_friendly_seeds_dict_with_questions(monkeypatch) -> None:
    """LLM returns {"questions": [...]} — should be unwrapped."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [json.dumps({"questions": ["q1", "q2"]})])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_friendly_seeds(["topic 1", "topic 2"])
    assert out == ["q1", "q2"]


def test_ghost_generate_friendly_seeds_dict_wrong_len_fallback(monkeypatch) -> None:
    """Mismatched length → falls back to generic seeds."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [json.dumps([{"q": "x"}])])  # not a flat list of len 2
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_friendly_seeds(["topic a", "topic b"])
    # Fallback: generic seeds (one per opp)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# conduct_interview — fast-path skipped via cancellation
# ---------------------------------------------------------------------------


def test_ghost_conduct_interview_cancels_immediately(monkeypatch) -> None:
    """When the job is already cancelled, conduct_interview returns skipped=True."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    # Always indicate the pipeline is waiting (so we enter the loop), but the job is cancelled.
    def fake_is_waiting(job_id):
        return True

    def fake_get_job(job_id):
        return {"status": "cancelled", "story_chat_history": [], "current_story_gap_index": 0}

    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "is_waiting_for_story_input", fake_is_waiting)
    monkeypatch.setattr(bjs, "get_blog_job", fake_get_job)

    # Stub out event bus — we don't want a real subscription
    fake_sub = MagicMock()
    fake_sub.notify.wait = lambda timeout=0: None
    fake_sub.notify.clear = lambda: None
    fake_sub.touch = lambda: None
    from shared import job_event_bus as bus

    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    result = agent.conduct_interview(gap=_gap(), job_id="job-1", gap_index=0, max_rounds=3)
    assert result.skipped is True


def test_ghost_conduct_interview_skipped_via_index_advance(monkeypatch) -> None:
    """When gap index advances past gap_index, return skipped."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    # Make is_waiting return False so we skip the inner wait loop;
    # then job_data shows current_story_gap_index > gap_index
    def fake_is_waiting(job_id):
        return False

    def fake_get_job(job_id):
        return {
            "status": "running",
            "story_chat_history": [],
            "current_story_gap_index": 5,
            "current_gap_round": 0,
        }

    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "is_waiting_for_story_input", fake_is_waiting)
    monkeypatch.setattr(bjs, "get_blog_job", fake_get_job)

    fake_sub = MagicMock()
    fake_sub.notify.wait = lambda timeout=0: None
    fake_sub.notify.clear = lambda: None
    fake_sub.touch = lambda: None
    from shared import job_event_bus as bus

    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    result = agent.conduct_interview(gap=_gap(), job_id="job-1", gap_index=0, max_rounds=2)
    assert result.skipped is True


def test_ghost_conduct_interview_no_experience_quick_exit(monkeypatch) -> None:
    """If the user's last message is a no-experience phrase, return skipped."""
    from ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    def fake_is_waiting(job_id):
        return False

    def fake_get_job(job_id):
        return {
            "status": "running",
            "story_chat_history": [
                {"role": "user", "content": "skip", "gap_round": 0},
            ],
            "current_story_gap_index": 0,
            "current_gap_round": 0,
        }

    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "is_waiting_for_story_input", fake_is_waiting)
    monkeypatch.setattr(bjs, "get_blog_job", fake_get_job)

    fake_sub = MagicMock()
    fake_sub.notify.wait = lambda timeout=0: None
    fake_sub.notify.clear = lambda: None
    fake_sub.touch = lambda: None
    from shared import job_event_bus as bus

    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    result = agent.conduct_interview(gap=_gap(), job_id="job-1", gap_index=0, max_rounds=2)
    assert result.skipped is True
