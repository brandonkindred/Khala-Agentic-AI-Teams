"""Tests for BlogWriterAgent.plan_content + _complete_plan_json (planning loop)."""

from __future__ import annotations

from typing import Any

import pytest


def _agent_with_guidelines():
    from blog_writer_agent.agent import BlogWriterAgent

    from llm_service import DummyLLMClient

    return BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Style",
        brand_spec_content="Brand",
    )


def _valid_plan_dict() -> dict[str, Any]:
    return {
        "overarching_topic": "Topic X",
        "narrative_flow": "Flow",
        "sections": [
            {"title": "Intro", "coverage_description": "hook", "order": 0},
            {"title": "Body", "coverage_description": "depth", "order": 1},
            {"title": "Wrap", "coverage_description": "conclude", "order": 2},
            {"title": "Notes", "coverage_description": "extras", "order": 3},
        ],
        "title_candidates": [{"title": "A Title", "probability_of_success": 0.7}],
        "requirements_analysis": {
            "plan_acceptable": True,
            "scope_feasible": True,
            "research_gaps": [],
        },
    }


def _planning_input():
    from shared.content_plan import PlanningInput

    return PlanningInput(brief="hi", length_policy_context="ctx", research_digest="rd")


def _length_policy():
    from shared.content_profile import ContentProfile, resolve_length_policy

    return resolve_length_policy(content_profile=ContentProfile.standard_article)


def test_complete_plan_json_first_attempt_succeeds(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_call_agent_json",
        lambda self, p, **kw: {"summary": "ok"},
    )
    data, retries = a._complete_plan_json(
        "p", system="sys", on_llm_request=None, max_parse_retries=2
    )
    assert data == {"summary": "ok"}
    assert retries == 0


def test_complete_plan_json_falls_back_to_parse_json_object(monkeypatch) -> None:
    """First call returns empty dict; the second call (parse_json_object) parses successfully."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent_with_guidelines()
    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", lambda self, p, **kw: {})
    monkeypatch.setattr(
        BlogWriterAgent, "_call_json_raw", lambda self, p, system_prompt="": '{"a": 1}'
    )
    data, retries = a._complete_plan_json(
        "p", system="sys", on_llm_request=lambda m: None, max_parse_retries=2
    )
    assert data == {"a": 1}


def test_complete_plan_json_raises_after_retries(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent
    from shared.errors import PlanningError

    a = _agent_with_guidelines()

    def boom_json(self, p, **kw):
        raise ValueError("bad json")

    monkeypatch.setattr(BlogWriterAgent, "_call_agent_json", boom_json)
    monkeypatch.setattr(
        BlogWriterAgent, "_call_json_raw", lambda self, p, system_prompt="": "not json"
    )
    with pytest.raises(PlanningError):
        a._complete_plan_json("p", system="sys", on_llm_request=None, max_parse_retries=1)


def test_plan_content_converges_first_iteration(monkeypatch) -> None:
    """plan_content returns successfully when the first plan is acceptable."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_complete_plan_json",
        lambda self, p, *, system, on_llm_request, max_parse_retries: (_valid_plan_dict(), 0),
    )
    out = a.plan_content(_planning_input(), length_policy=_length_policy())
    assert out.planning_iterations_used == 1
    assert out.content_plan.overarching_topic == "Topic X"


def test_plan_content_invalid_schema_raises(monkeypatch) -> None:
    from blog_writer_agent.agent import BlogWriterAgent
    from shared.errors import PlanningError

    a = _agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_complete_plan_json",
        lambda self, p, *, system, on_llm_request, max_parse_retries: ({"garbage": 1}, 0),
    )
    with pytest.raises(PlanningError):
        a.plan_content(_planning_input(), length_policy=_length_policy())


def test_plan_content_refines_when_not_acceptable(monkeypatch) -> None:
    """When plan_acceptable=False, iterate once more then succeed."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent_with_guidelines()
    state = {"i": 0}

    def fake(self, p, *, system, on_llm_request, max_parse_retries):
        state["i"] += 1
        d = _valid_plan_dict()
        if state["i"] == 1:
            d["requirements_analysis"]["plan_acceptable"] = False
        return d, 0

    monkeypatch.setattr(BlogWriterAgent, "_complete_plan_json", fake)
    out = a.plan_content(_planning_input(), length_policy=_length_policy(), max_iterations=3)
    assert out.planning_iterations_used >= 1


def test_plan_content_fills_missing_title_candidates(monkeypatch) -> None:
    """If the plan has no title candidates, one is synthesised from topic."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent_with_guidelines()
    plan = _valid_plan_dict()
    plan["title_candidates"] = []
    monkeypatch.setattr(
        BlogWriterAgent,
        "_complete_plan_json",
        lambda self, p, *, system, on_llm_request, max_parse_retries: (plan, 0),
    )
    out = a.plan_content(_planning_input(), length_policy=_length_policy())
    assert len(out.content_plan.title_candidates) >= 1


def test_plan_content_with_plan_critic(monkeypatch) -> None:
    """plan_critic provided → critic.run is called and result merged."""
    from blog_writer_agent.agent import BlogWriterAgent

    a = _agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_complete_plan_json",
        lambda self, p, *, system, on_llm_request, max_parse_retries: (_valid_plan_dict(), 0),
    )

    class _Critic:
        def run(self, **kw):
            class _Report:
                approved = True

                def to_dict(self):
                    return {"status": "approved"}

            return _Report()

    out = a.plan_content(_planning_input(), length_policy=_length_policy(), plan_critic=_Critic())
    assert out.plan_critic_report == {"status": "approved"}


def test_plan_content_max_iterations_exhausted(monkeypatch) -> None:
    """Critic never approves → PlanningError raised after max_iterations."""
    from blog_writer_agent.agent import BlogWriterAgent
    from shared.errors import PlanningError

    a = _agent_with_guidelines()
    monkeypatch.setattr(
        BlogWriterAgent,
        "_complete_plan_json",
        lambda self, p, *, system, on_llm_request, max_parse_retries: (_valid_plan_dict(), 0),
    )

    class _Critic:
        def run(self, **kw):
            class _Report:
                approved = False
                violations = []

                def to_dict(self):
                    return {"status": "rejected"}

            return _Report()

    with pytest.raises(PlanningError):
        a.plan_content(
            _planning_input(),
            length_policy=_length_policy(),
            plan_critic=_Critic(),
            max_iterations=2,
        )
