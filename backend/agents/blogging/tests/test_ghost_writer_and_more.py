"""More targeted tests for ghost_writer_agent and adjacent modules.

Covers:
* ``GhostWriterElicitationAgent._evaluate_sufficiency`` with parse failure + retry success.
* ``_generate_follow_up`` and ``_compile_narrative`` happy + error paths.
* ``_find_gaps_via_llm`` happy + error paths.
* ``_plan_to_text``.
* ``conduct_interview`` quick exits (skipped, cancelled, no-experience).
* ``_is_no_experience`` (exhaustive true/false cases), agent construction,
  ``_extract_gaps_from_plan``, and ``_generate_friendly_seeds``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest


def _content_plan():
    """Build a minimal 2-section ContentPlan fixture shared by tests in this file."""
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    return make_content_plan(
        overarching_topic="Building scalable APIs",
        narrative_flow="Intro, body, wrap",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="hook", order=0),
            ContentPlanSection(title="Body", coverage_description="depth", order=1),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )


def _gap():
    """Build a single StoryGap fixture shared by tests in this file."""
    from agents.blogging.ghost_writer_agent.models import StoryGap

    return StoryGap(
        section_title="Intro",
        section_context="Hook the reader",
        seed_question="Got a story?",
    )


# ---------------------------------------------------------------------------
# _JSON_RETRY_SUFFIX — shape neutrality
# ---------------------------------------------------------------------------


def test_json_retry_suffix_is_shape_agnostic() -> None:
    """Retry suffix must not demand a JSON object (gap-finding returns an array)."""
    from agents.blogging.ghost_writer_agent.agent import _JSON_RETRY_SUFFIX

    assert _JSON_RETRY_SUFFIX == (
        "\n\nRespond with valid JSON only (no markdown, no code fences)."
    )
    assert "object" not in _JSON_RETRY_SUFFIX.lower()


# ---------------------------------------------------------------------------
# _plan_to_text
# ---------------------------------------------------------------------------


def test_ghost_plan_to_text_renders_sections() -> None:
    """_plan_to_text renders the topic and each section's title/coverage."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    plan = _content_plan()
    text = GhostWriterElicitationAgent._plan_to_text(plan)
    assert "Topic/thesis: Building scalable APIs" in text
    assert "Section: Intro" in text
    assert "Coverage: hook" in text
    assert "Section: Body" in text
    assert "Coverage: depth" in text


# ---------------------------------------------------------------------------
# _evaluate_sufficiency — parse failure retry + success
# ---------------------------------------------------------------------------


def _patch_agent(monkeypatch, responses: List[Any], target_module: Any = None) -> Dict[str, int]:
    """Stub the strands Agent class used by ghost_writer_agent.agent.

    ``target_module`` defaults to ``ghost_writer_agent.agent`` (used by
    ``_generate_follow_up``/``_compile_narrative``, which construct ``Agent``
    directly) — pass ``agents.blogging.shared.json_retry`` for call sites that
    go through ``run_json_gate`` (``_evaluate_sufficiency``/``_find_gaps_via_llm``),
    since that helper constructs its ``Agent`` in its own module.

    Returns the shared call-count state dict (key ``"i"``) so callers can assert
    exactly how many of the configured ``responses`` were consumed. Calling the
    stub more times than ``len(responses)`` raises ``AssertionError`` rather than
    silently repeating the last response, so an implementation that calls the
    agent more times than a test expects fails loudly instead of masking the
    extra call behind a reused response.
    """
    if target_module is None:
        import agents.blogging.ghost_writer_agent.agent as target_module

    state = {"i": 0}

    class _StubAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt: str) -> str:
            if state["i"] >= len(responses):
                raise AssertionError(
                    f"Agent called {state['i'] + 1} times, but only {len(responses)} responses configured"
                )
            r = responses[state["i"]]
            state["i"] += 1
            if isinstance(r, Exception):
                raise r
            return r

    monkeypatch.setattr(target_module, "Agent", _StubAgent)
    return state


def test_ghost_evaluate_sufficiency_success(monkeypatch) -> None:
    """A single valid JSON response is parsed and returned as-is."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

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
        target_module=json_retry,
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [{"role": "agent", "content": "Got a story?"}])
    assert out["sufficient"] is True
    assert out["story_context"] == "client"


def test_ghost_evaluate_sufficiency_parse_retry_succeeds(monkeypatch) -> None:
    """An unparseable first response is retried once; the second, valid response is used."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient

    state = _patch_agent(
        monkeypatch,
        [
            "not-json",
            json.dumps(
                {"sufficient": True, "no_experience": False, "story_context": None, "missing": None}
            ),
        ],
        target_module=json_retry,
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out["sufficient"] is True
    assert state["i"] == 2


def test_ghost_evaluate_sufficiency_falls_back_default(monkeypatch) -> None:
    """Two unparseable responses exhaust the retry budget; the default sufficiency dict is returned."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient

    state = _patch_agent(monkeypatch, ["not-json-1", "not-json-2"], target_module=json_retry)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out == {
        "sufficient": False,
        "no_experience": False,
        "story_context": None,
        "missing": None,
    }
    assert state["i"] == 2


def test_ghost_evaluate_sufficiency_exception_then_default(monkeypatch) -> None:
    """A non-transient exception from the agent falls back to the default sufficiency dict."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("LLM exploded")

    monkeypatch.setattr(json_retry, "Agent", _Boom)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out == {
        "sufficient": False,
        "no_experience": False,
        "story_context": None,
        "missing": None,
    }


def test_ghost_evaluate_sufficiency_rate_limit_falls_back_default(monkeypatch) -> None:
    """LLMRateLimitError from the evaluator is caught and the default not-sufficient dict is
    returned, matching the behavior for other transient failures."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient, LLMRateLimitError

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(json_retry, "Agent", _Boom)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._evaluate_sufficiency(_gap(), [])
    assert out == {
        "sufficient": False,
        "no_experience": False,
        "story_context": None,
        "missing": None,
    }


# ---------------------------------------------------------------------------
# _generate_follow_up
# ---------------------------------------------------------------------------


def test_ghost_generate_follow_up_happy(monkeypatch) -> None:
    """A single well-formed response is returned verbatim as the follow-up question."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

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
    """An agent exception is swallowed; _generate_follow_up returns None instead of raising."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [RuntimeError("nope")])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_follow_up(_gap(), [], {})
    assert out is None


def test_ghost_generate_follow_up_empty_response(monkeypatch) -> None:
    """A whitespace-only response is treated as no follow-up question (returns None)."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["   "])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._generate_follow_up(_gap(), [], {}) is None


# ---------------------------------------------------------------------------
# _compile_narrative
# ---------------------------------------------------------------------------


def test_ghost_compile_narrative_empty_user_content(monkeypatch) -> None:
    """No non-empty user turns in the conversation short-circuits to None (no LLM call)."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    state = _patch_agent(monkeypatch, [])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._compile_narrative(_gap(), [{"role": "agent", "content": "hi"}])
    assert out is None
    assert state["i"] == 0


def test_ghost_compile_narrative_happy_path_with_context(monkeypatch) -> None:
    """A successful narrator call returns the compiled narrative text unchanged."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

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
    """The narrator failing on every retry attempt returns None instead of raising."""
    import agents.blogging.ghost_writer_agent.agent as gw_agent
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

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
    """A valid JSON array of gap objects is parsed; a blank seed_question gets a generated fallback."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

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
        target_module=json_retry,
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._find_gaps_via_llm(plan)
    assert len(out) == 2
    assert out[0].seed_question == "Tell me a moment"
    # Second gap has a fallback question
    assert "deep dive" in out[1].seed_question.lower()


def test_ghost_find_gaps_via_llm_no_array_returns_empty(monkeypatch) -> None:
    """A response with no JSON array parses to a non-list value, so an empty gap list is returned."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, ["no brackets here"], target_module=json_retry)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._find_gaps_via_llm(_content_plan())
    assert out == []


def test_ghost_find_gaps_via_llm_parse_error_retry_then_fail(monkeypatch) -> None:
    """Two unparseable array responses exhaust the retry budget; an empty gap list is returned."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient

    state = _patch_agent(monkeypatch, ["[not-json", "[also-not-json"], target_module=json_retry)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._find_gaps_via_llm(_content_plan()) == []
    assert state["i"] == 2


def test_ghost_find_gaps_via_llm_exception_falls_back_empty(monkeypatch) -> None:
    """Generic invoke errors fall back immediately (shared helper, no local retry)."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

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

    monkeypatch.setattr(json_retry, "Agent", _Stub)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # Old loop would recover on attempt 2; helper falls back on first unexpected error.
    assert agent._find_gaps_via_llm(_content_plan()) == []


def test_ghost_find_gaps_via_llm_skips_non_dict_items(monkeypatch) -> None:
    """Array items that are not dicts are silently dropped; valid dicts are kept."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient

    _patch_agent(
        monkeypatch,
        [
            json.dumps(
                [
                    "not-an-object",
                    {
                        "section_title": "Intro",
                        "section_context": "Hook",
                        "seed_question": "Got a moment?",
                    },
                ]
            )
        ],
        target_module=json_retry,
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._find_gaps_via_llm(_content_plan())
    assert len(out) == 1
    assert out[0].seed_question == "Got a moment?"


def test_ghost_find_gaps_via_llm_coerces_null_and_non_string_fields(monkeypatch) -> None:
    """Null seed_question triggers a generated fallback question that incorporates the
    stringified section_context; null section_title is normalized to an empty string,
    and non-null non-string fields (section_context) are coerced to strings."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient

    _patch_agent(
        monkeypatch,
        [
            json.dumps(
                [
                    {
                        "section_title": None,
                        "section_context": 42,
                        "seed_question": None,
                    }
                ]
            )
        ],
        target_module=json_retry,
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._find_gaps_via_llm(_content_plan())
    assert len(out) == 1
    assert out[0].section_title == ""
    assert out[0].section_context == "42"
    assert "42" in out[0].seed_question


def test_ghost_find_gaps_via_llm_rate_limit_falls_back_empty(monkeypatch) -> None:
    """Soft call sites map transient LLM errors to [] (planning_stage would otherwise swallow)."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared import json_retry

    from llm_service import DummyLLMClient, LLMRateLimitError

    class _Boom:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise LLMRateLimitError("rate limited")

    monkeypatch.setattr(json_retry, "Agent", _Boom)
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._find_gaps_via_llm(_content_plan()) == []


def test_ghost_find_story_gaps_uses_plan_opportunities_when_present(monkeypatch) -> None:
    """find_story_gaps short-circuits to opportunities, avoiding LLM gap-finding."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from llm_service import DummyLLMClient

    from ._content_plan_test_utils import make_content_plan

    sec = ContentPlanSection(
        title="A", coverage_description="cov", order=0, story_opportunity="A bug story"
    )
    plan = make_content_plan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[sec],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    monkeypatch.setattr(agent, "_generate_friendly_seeds", lambda opps: [f"q-{o}" for o in opps])
    out = agent.find_story_gaps(plan)
    assert len(out) == 1
    assert out[0].seed_question.startswith("q-A bug story")


def test_ghost_find_story_gaps_falls_back_to_llm(monkeypatch) -> None:
    """No story_opportunity → goes through _find_gaps_via_llm."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    monkeypatch.setattr(agent, "_find_gaps_via_llm", lambda plan: ["sentinel"])
    out = agent.find_story_gaps(_content_plan())
    assert out == ["sentinel"]


# ---------------------------------------------------------------------------
# _generate_friendly_seeds — dict and list response forms
# ---------------------------------------------------------------------------


def test_ghost_generate_friendly_seeds_dict_with_questions(monkeypatch) -> None:
    """LLM returns {"questions": [...]} — should be unwrapped."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [json.dumps({"questions": ["q1", "q2"]})])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_friendly_seeds(["topic 1", "topic 2"])
    assert out == ["q1", "q2"]


def test_ghost_generate_friendly_seeds_dict_wrong_len_fallback(monkeypatch) -> None:
    """LLM returns a dict whose questions list has the wrong length → falls back to generic seeds."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [json.dumps({"questions": ["only-one"]})])
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_friendly_seeds(["topic a", "topic b"])
    # Fallback: generic seeds (one per opp)
    assert len(out) == 2
    assert all("topic" in s.lower() for s in out)


def test_ghost_generate_friendly_seeds_list_wrong_len_fallback(monkeypatch) -> None:
    """LLM returns a JSON list of wrong length → falls back to generic seeds."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [json.dumps([{"q": "x"}])])  # not a flat list of len 2
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_friendly_seeds(["topic a", "topic b"])
    # Fallback: generic seeds (one per opp)
    assert len(out) == 2
    assert all("topic" in s.lower() for s in out)


def test_ghost_generate_friendly_seeds_non_string_items_fallback(monkeypatch) -> None:
    """LLM returns a right-length list of non-string items → falls back to generic seeds
    instead of returning stringified garbage (e.g. "42", "{'q': 'x'}")."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    _patch_agent(monkeypatch, [json.dumps([{"q": "x"}, 42])])  # right length, wrong item types
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    out = agent._generate_friendly_seeds(["topic a", "topic b"])
    # Fallback: generic seeds (one per opp), not stringified dict/int garbage
    assert len(out) == 2
    assert all("topic" in s.lower() for s in out)
    assert not any(s in ("42", "{'q': 'x'}") for s in out)


# ---------------------------------------------------------------------------
# conduct_interview — fast-path skip cases (cancellation, index-advance, no-experience)
# ---------------------------------------------------------------------------


def test_ghost_conduct_interview_cancels_immediately(monkeypatch) -> None:
    """When the job is already cancelled, conduct_interview returns skipped=True."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    # Always indicate the pipeline is waiting (so we enter the loop), but the job is cancelled.
    def fake_is_waiting(job_id):
        return True

    def fake_get_job(job_id):
        return {"status": "cancelled", "story_chat_history": [], "current_story_gap_index": 0}

    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "is_waiting_for_story_input", fake_is_waiting)
    monkeypatch.setattr(bjs, "get_blog_job", fake_get_job)

    # Stub out event bus — we don't want a real subscription
    fake_sub = MagicMock()
    fake_sub.notify.wait = lambda timeout=0: None
    fake_sub.notify.clear = lambda: None
    fake_sub.touch = lambda: None
    from agents.blogging.shared import job_event_bus as bus

    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    result = agent.conduct_interview(gap=_gap(), job_id="job-1", gap_index=0, max_rounds=3)
    assert result.skipped is True


def test_ghost_conduct_interview_skipped_via_index_advance(monkeypatch) -> None:
    """When gap index advances past gap_index, return skipped."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

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

    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "is_waiting_for_story_input", fake_is_waiting)
    monkeypatch.setattr(bjs, "get_blog_job", fake_get_job)

    fake_sub = MagicMock()
    fake_sub.notify.wait = lambda timeout=0: None
    fake_sub.notify.clear = lambda: None
    fake_sub.touch = lambda: None
    from agents.blogging.shared import job_event_bus as bus

    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    result = agent.conduct_interview(gap=_gap(), job_id="job-1", gap_index=0, max_rounds=2)
    assert result.skipped is True


def test_ghost_conduct_interview_no_experience_quick_exit(monkeypatch) -> None:
    """If the user's last message is a no-experience phrase, return skipped."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

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

    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "is_waiting_for_story_input", fake_is_waiting)
    monkeypatch.setattr(bjs, "get_blog_job", fake_get_job)

    fake_sub = MagicMock()
    fake_sub.notify.wait = lambda timeout=0: None
    fake_sub.notify.clear = lambda: None
    fake_sub.touch = lambda: None
    from agents.blogging.shared import job_event_bus as bus

    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    result = agent.conduct_interview(gap=_gap(), job_id="job-1", gap_index=0, max_rounds=2)
    assert result.skipped is True


# ---------------------------------------------------------------------------
# _is_no_experience
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "I have a passion for this",
        "None of that applied",
        "I am skipping ahead",
        "nonetheless, it was fine",
        "I have no time",
        "please skip ahead to the next part",
    ],
)
def test_is_no_experience_false_positives(message: str) -> None:
    """Ordinary prose containing 'pass'/'none'/'skip'/'no' substrings must not match."""
    from agents.blogging.ghost_writer_agent.agent import _is_no_experience

    assert _is_no_experience(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "skip",
        "skip.",
        "none",
        "pass",
        "skip this one",
        "n/a for now",
        "I have no experience",
        "I have no relevant experience",
        "I don't have a story",
        "no relevant experience",
    ],
)
def test_is_no_experience_true_positives(message: str) -> None:
    """Intended no-experience refusal signals must still be detected."""
    from agents.blogging.ghost_writer_agent.agent import _is_no_experience

    assert _is_no_experience(message) is True


# ---------------------------------------------------------------------------
# _notify_job_updater
# ---------------------------------------------------------------------------


def test_notify_job_updater_noop_when_none() -> None:
    """_notify_job_updater is a no-op when job_updater is None."""
    from agents.blogging.ghost_writer_agent.agent import _notify_job_updater

    _notify_job_updater(None, status_text="unused")


def test_notify_job_updater_invokes_callback() -> None:
    """_notify_job_updater forwards kwargs to the callback."""
    from agents.blogging.ghost_writer_agent.agent import _notify_job_updater

    calls: list[dict] = []
    _notify_job_updater(lambda **kw: calls.append(kw), status_text="hi", phase="story_elicitation")
    assert calls == [{"status_text": "hi", "phase": "story_elicitation"}]


def test_notify_job_updater_swallows_errors() -> None:
    """Progress-callback failures are logged and do not raise."""
    from agents.blogging.ghost_writer_agent.agent import _notify_job_updater

    def boom(**kwargs):
        raise RuntimeError("store down")

    _notify_job_updater(boom, status_text="x")


def test_notify_job_updater_reraises_cancelled() -> None:
    """CancelledError from the callback must propagate."""
    from agents.blogging.ghost_writer_agent.agent import _notify_job_updater
    from temporalio.exceptions import CancelledError

    def cancel(**kwargs):
        raise CancelledError("stop")

    with pytest.raises(CancelledError):
        _notify_job_updater(cancel, status_text="x")


def test_ghost_conduct_interview_notifies_job_updater(monkeypatch) -> None:
    """conduct_interview invokes job_updater while waiting for a quick no-experience exit."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    waiting = {"n": 0}

    def fake_is_waiting(job_id):
        waiting["n"] += 1
        return waiting["n"] == 1

    def fake_get_job(job_id):
        return {
            "status": "running",
            "story_chat_history": [
                {"role": "user", "content": "skip", "gap_round": 0},
            ],
            "current_story_gap_index": 0,
            "current_gap_round": 0,
        }

    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "is_waiting_for_story_input", fake_is_waiting)
    monkeypatch.setattr(bjs, "get_blog_job", fake_get_job)

    fake_sub = MagicMock()
    fake_sub.notify.wait = lambda timeout=0: None
    fake_sub.notify.clear = lambda: None
    fake_sub.touch = lambda: None
    from agents.blogging.shared import job_event_bus as bus

    monkeypatch.setattr(bus, "subscribe", lambda jid: fake_sub)
    monkeypatch.setattr(bus, "unsubscribe", lambda jid, sub: None)

    updates: list[dict] = []
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    result = agent.conduct_interview(
        gap=_gap(),
        job_id="job-1",
        gap_index=0,
        job_updater=lambda **kw: updates.append(kw),
        max_rounds=2,
    )
    assert result.skipped is True
    assert updates
    assert any("Waiting for your response" in u.get("status_text", "") for u in updates)


# ---------------------------------------------------------------------------
# _is_no_experience (exhaustive), construction, _extract_gaps_from_plan,
# _generate_friendly_seeds — moved here from test_writer_and_v2_helpers.py to
# keep ghost_writer_agent coverage in one file.
# ---------------------------------------------------------------------------


def test_ghost_writer_no_experience_phrase() -> None:
    from agents.blogging.ghost_writer_agent.agent import _is_no_experience

    # Exact short tokens
    assert _is_no_experience("skip") is True
    assert _is_no_experience("SKIP.") is True
    assert _is_no_experience("none") is True
    assert _is_no_experience("pass") is True
    assert _is_no_experience("n/a") is True

    # Formerly ambiguous stems — exact message only (not substring)
    assert _is_no_experience("Nothing comes to mind") is True
    assert _is_no_experience("nothing comes to mind.") is True
    assert _is_no_experience("I haven't done that") is True
    assert _is_no_experience("i haven't") is True
    assert _is_no_experience("i have no") is True
    assert _is_no_experience("i can't think of") is True

    # Explicit command-prefixed skips (leading token + trailing text)
    assert _is_no_experience("skip this one") is True
    assert _is_no_experience("skip, please") is True
    assert _is_no_experience("pass on this question") is True
    assert _is_no_experience("n/a for this section") is True

    # Specific refusal phrases (word-boundary containment)
    assert _is_no_experience("I don't have any story") is True
    assert _is_no_experience("I don't have direct experience with that") is True
    assert _is_no_experience("I don't have any relevant experiences") is True
    assert _is_no_experience("no relevant experience for this") is True
    assert _is_no_experience("I have no experience with that") is True
    assert _is_no_experience("I have no story for this topic") is True
    assert _is_no_experience("I can't think of a story") is True
    assert _is_no_experience("Yes I have a great one") is False

    # Qualified experience refusals (optional adjective between "no" and "experience")
    assert _is_no_experience("I have no direct experience with that") is True
    assert _is_no_experience("I have no personal experience here") is True
    assert _is_no_experience("I have no relevant experiences in this area") is True
    assert _is_no_experience("I have no prior experience") is True

    # Ambiguous substrings / incidental short-word uses must NOT skip
    assert _is_no_experience("I have no idea what you mean") is False
    assert _is_no_experience("I haven't thought about it that way") is False
    assert _is_no_experience("I can't think of anything else right now") is False
    assert _is_no_experience("Nothing comes to mind immediately") is False
    assert _is_no_experience("I haven't tried that") is False
    assert _is_no_experience("nothing comes to mind here") is False
    assert _is_no_experience("please skip ahead in the draft") is False
    assert _is_no_experience("I will pass along the details") is False
    assert _is_no_experience("none of my colleagues knew the answer, but I did") is False
    assert (
        _is_no_experience("I don't have the exact dates, but the migration started after launch")
        is False
    )


def test_ghost_writer_agent_construction() -> None:
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent is not None


def test_ghost_writer_extract_gaps_from_plan_no_opportunities() -> None:
    """_extract_gaps_from_plan returns an empty list when no section has a
    story_opportunity; no LLM call is made."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from llm_service import DummyLLMClient

    from ._content_plan_test_utils import make_content_plan

    plan = make_content_plan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="A", coverage_description="cov", order=0),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )
    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # When no story_opportunity on sections, _extract_gaps_from_plan returns []
    out = agent._extract_gaps_from_plan(plan)
    assert out == []


def test_ghost_writer_extract_gaps_from_plan_with_opportunities(monkeypatch) -> None:
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from llm_service import DummyLLMClient

    from ._content_plan_test_utils import make_content_plan

    sec_a = ContentPlanSection(
        title="A", coverage_description="cov", order=0, story_opportunity="A debug story"
    )
    sec_b = ContentPlanSection(
        title="B",
        coverage_description="cov2",
        order=1,
        story_opportunity="A migration story",
    )
    plan = make_content_plan(
        overarching_topic="X",
        narrative_flow="flow",
        sections=[sec_a, sec_b],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    # Patch the seed generator to avoid LLM call
    monkeypatch.setattr(agent, "_generate_friendly_seeds", lambda opps: [f"seed-{o}" for o in opps])
    out = agent._extract_gaps_from_plan(plan)
    assert len(out) == 2
    assert out[0].section_title == "A"
    assert "seed-A debug story" == out[0].seed_question
    assert out[1].section_title == "B"
    assert out[1].seed_question == "seed-A migration story"


def test_ghost_writer_generate_friendly_seeds_fallback(monkeypatch) -> None:
    """When the LLM call raises, _generate_friendly_seeds falls back to generic seeds."""
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())

    # Patch the Agent class globally inside ghost_writer_agent.agent
    import agents.blogging.ghost_writer_agent.agent as gw_agent

    class _BoomAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise RuntimeError("nope")

    monkeypatch.setattr(gw_agent, "Agent", _BoomAgent)
    out = agent._generate_friendly_seeds(["topic A.", "topic B."])
    assert len(out) == 2
    assert all("topic" in s.lower() for s in out)


def test_ghost_writer_generate_friendly_seeds_empty_input() -> None:
    from agents.blogging.ghost_writer_agent.agent import GhostWriterElicitationAgent

    from llm_service import DummyLLMClient

    agent = GhostWriterElicitationAgent(llm_client=DummyLLMClient())
    assert agent._generate_friendly_seeds([]) == []
