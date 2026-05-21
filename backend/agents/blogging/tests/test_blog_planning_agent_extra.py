"""Targeted extra coverage for ``blog_planning_agent.agent`` paths."""

from __future__ import annotations

import json
from typing import Any

import pytest
from blog_planning_agent.agent import (
    BlogPlanningAgent,
    _build_generate_prompt,
    _build_refine_prompt,
)
from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    PlanningInput,
    RequirementsAnalysis,
    TitleCandidate,
)
from shared.content_profile import ContentProfile, resolve_length_policy
from shared.errors import PlanningError


def _policy_standard():
    return resolve_length_policy(content_profile=ContentProfile.standard_article)


def _good_plan_dict() -> dict[str, Any]:
    return {
        "overarching_topic": "Topic",
        "narrative_flow": "x then y",
        "sections": [
            {"title": "A", "coverage_description": "doA", "order": 0},
            {"title": "B", "coverage_description": "doB", "order": 1},
            {"title": "C", "coverage_description": "doC", "order": 2},
            {"title": "D", "coverage_description": "doD", "order": 3},
        ],
        "title_candidates": [{"title": "T", "probability_of_success": 0.7}],
        "requirements_analysis": {
            "plan_acceptable": True,
            "scope_feasible": True,
            "research_gaps": [],
        },
    }


def _bad_plan_dict() -> dict[str, Any]:
    d = _good_plan_dict()
    d["requirements_analysis"]["plan_acceptable"] = False
    return d


def test_build_generate_prompt_with_optional_fields() -> None:
    inp = PlanningInput(
        brief="A brief",
        audience="audience-x",
        tone_or_purpose="tone-y",
        length_policy_context="ctx",
        research_digest="digest",
        series_context_block="series block content",
    )
    out = _build_generate_prompt(inp)
    assert "audience-x" in out
    assert "tone-y" in out
    assert "series block content" in out


def test_build_generate_prompt_skips_blank_series_block() -> None:
    inp = PlanningInput(
        brief="A brief",
        length_policy_context="ctx",
        research_digest="digest",
        series_context_block="   ",
    )
    out = _build_generate_prompt(inp)
    assert "series" not in out.lower()


def test_build_refine_prompt_includes_previous_plan_and_feedback() -> None:
    inp = PlanningInput(
        brief="b",
        length_policy_context="ctx",
        research_digest="digest",
    )
    prev = ContentPlan(
        overarching_topic="t",
        narrative_flow="n",
        sections=[ContentPlanSection(title="x", coverage_description="x", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=False,
            scope_feasible=True,
            research_gaps=[],
        ),
    )
    out = _build_refine_prompt(inp, prev, "fix gaps")
    assert "fix gaps" in out
    assert "PREVIOUS PLAN" in out


def test_complete_plan_json_recovers_on_parse_retry(monkeypatch) -> None:
    """First call returns invalid JSON, fallback parse_json_object succeeds."""
    from llm_service import DummyLLMClient

    agent = BlogPlanningAgent(DummyLLMClient())
    good = _good_plan_dict()

    responses = iter([
        "not json at all",
        json.dumps(good),
    ])

    def fake_call(self, prompt: str, system: str) -> str:
        return next(responses)

    monkeypatch.setattr(BlogPlanningAgent, "_call_agent", fake_call)

    seen: list[str] = []
    data, retries = agent._complete_plan_json(
        prompt="p",
        system="s",
        on_llm_request=seen.append,
        max_parse_retries=2,
    )
    assert data["overarching_topic"] == "Topic"
    assert retries == 1
    assert any("Planning" in s for s in seen)


def test_complete_plan_json_raises_after_max_parse_retries(monkeypatch) -> None:
    from llm_service import DummyLLMClient

    agent = BlogPlanningAgent(DummyLLMClient())

    def fake_call(self, prompt: str, system: str) -> str:
        return "completely bogus output"

    monkeypatch.setattr(BlogPlanningAgent, "_call_agent", fake_call)

    with pytest.raises(PlanningError) as exc:
        agent._complete_plan_json(
            prompt="p",
            system="s",
            on_llm_request=None,
            max_parse_retries=2,
        )
    assert "parse failed" in str(exc.value).lower()


def test_run_refines_with_critic_feedback(monkeypatch) -> None:
    """Iteration 2 uses critic feedback (last_critic_report path)."""
    from blog_plan_critic_agent.models import PlanCriticReport

    from llm_service import DummyLLMClient

    bad = _bad_plan_dict()
    good = _good_plan_dict()
    plans = iter([json.dumps(bad), json.dumps(good)])

    def fake_call(self, prompt: str, system: str) -> str:
        return next(plans)

    monkeypatch.setattr(BlogPlanningAgent, "_call_agent", fake_call)

    class _Critic:
        def __init__(self):
            self.called = 0

        def run(self, **_kw):
            self.called += 1
            return PlanCriticReport(
                status="PASS" if self.called > 1 else "FAIL",
                approved=self.called > 1,
                violations=[],
                notes="critic-feedback",
            )

    critic = _Critic()
    agent = BlogPlanningAgent(DummyLLMClient(), plan_critic=critic)
    result = agent.run(
        PlanningInput(brief="b", length_policy_context="c", research_digest="d"),
        length_policy=_policy_standard(),
    )
    assert result.planning_iterations_used == 2
    assert critic.called == 2


def test_run_refines_without_critic_uses_default_feedback(monkeypatch) -> None:
    from llm_service import DummyLLMClient

    bad = _bad_plan_dict()
    good = _good_plan_dict()
    plans = iter([json.dumps(bad), json.dumps(good)])

    def fake_call(self, prompt: str, system: str) -> str:
        return next(plans)

    monkeypatch.setattr(BlogPlanningAgent, "_call_agent", fake_call)

    agent = BlogPlanningAgent(DummyLLMClient())
    result = agent.run(
        PlanningInput(brief="b", length_policy_context="c", research_digest="d"),
        length_policy=_policy_standard(),
        max_iterations=3,
    )
    assert result.planning_iterations_used == 2


def test_run_schema_validation_failure_raises(monkeypatch) -> None:
    from llm_service import DummyLLMClient

    def fake_call(self, prompt: str, system: str) -> str:
        return json.dumps({"not": "a plan"})

    monkeypatch.setattr(BlogPlanningAgent, "_call_agent", fake_call)
    agent = BlogPlanningAgent(DummyLLMClient())

    with pytest.raises(PlanningError) as exc:
        agent.run(
            PlanningInput(brief="b", length_policy_context="c", research_digest="d"),
            length_policy=_policy_standard(),
        )
    assert "schema" in str(exc.value).lower() or "invalid" in str(exc.value).lower()


def test_run_fills_default_title_candidate_when_missing(monkeypatch) -> None:
    from llm_service import DummyLLMClient

    good = _good_plan_dict()
    good["title_candidates"] = []
    good["overarching_topic"] = "An example topic"

    def fake_call(self, prompt: str, system: str) -> str:
        return json.dumps(good)

    monkeypatch.setattr(BlogPlanningAgent, "_call_agent", fake_call)
    agent = BlogPlanningAgent(DummyLLMClient())
    result = agent.run(
        PlanningInput(brief="b", length_policy_context="c", research_digest="d"),
        length_policy=_policy_standard(),
    )
    assert result.content_plan.title_candidates
    assert "An example topic" in result.content_plan.title_candidates[0].title


def test_run_raises_after_max_iterations_no_convergence(monkeypatch) -> None:
    from llm_service import DummyLLMClient

    bad = _bad_plan_dict()

    def fake_call(self, prompt: str, system: str) -> str:
        return json.dumps(bad)

    monkeypatch.setattr(BlogPlanningAgent, "_call_agent", fake_call)
    agent = BlogPlanningAgent(DummyLLMClient())
    with pytest.raises(PlanningError) as exc:
        agent.run(
            PlanningInput(brief="b", length_policy_context="c", research_digest="d"),
            length_policy=_policy_standard(),
            max_iterations=2,
        )
    assert "did not converge" in str(exc.value)
