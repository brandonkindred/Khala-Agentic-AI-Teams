"""Tests for the independent plan critic.

These tests drive the critic directly via a fake Strands Agent so we can
exercise the full parse → coerce → report path without hitting an LLM, plus
an integration test that runs the critic inside BlogPlanningAgent.run.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from blog_plan_critic_agent import BlogPlanCriticAgent, PlanCriticReport
from blog_plan_critic_agent.agent import build_refine_feedback_from_critic
from blog_planning_agent import BlogPlanningAgent
from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    PlanningInput,
    RequirementsAnalysis,
    TitleCandidate,
)
from shared.content_profile import ContentProfile, LengthPolicy, resolve_length_policy

from llm_service import DummyLLMClient


def _policy_standard() -> LengthPolicy:
    return resolve_length_policy(content_profile=ContentProfile.standard_article)


def _minimal_plan(topic: str = "A stance about X that readers should adopt") -> ContentPlan:
    return ContentPlan(
        overarching_topic=topic,
        narrative_flow="Reader journey from skepticism to conviction.",
        sections=[
            ContentPlanSection(
                title=f"S{i}",
                coverage_description="Specific coverage.",
                key_points=[f"Point {i}A", f"Point {i}B", f"Point {i}C"],
                what_to_avoid=["Generic advice"],
                reader_takeaway="After this section, the reader believes X.",
                strongest_point="The specific hill to die on.",
                opening_hook="A concrete question.",
                transition_to_next="Tension that leads forward.",
                order=i,
            )
            for i in range(4)
        ],
        title_candidates=[
            TitleCandidate(title=f"Title candidate {i}", probability_of_success=0.7)
            for i in range(5)
        ],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True,
            scope_feasible=True,
            research_gaps=[],
        ),
    )


class _FakeAgent:
    """Drop-in replacement for strands.Agent that returns a canned response.

    Tracks every call so tests can assert how many critic passes ran.
    """

    calls: list[tuple[str, str]] = []
    responses: list[str] = []

    def __init__(self, model: Any, system_prompt: str = "") -> None:
        self._system = system_prompt

    def __call__(self, user_prompt: str) -> str:
        _FakeAgent.calls.append((self._system, user_prompt))
        if _FakeAgent.responses:
            return _FakeAgent.responses.pop(0)
        return json.dumps(
            {
                "status": "PASS",
                "approved": True,
                "violations": [],
                "notes": None,
                "rubric_version": "v1",
            }
        )


@pytest.fixture(autouse=True)
def _reset_fake_agent():
    _FakeAgent.calls = []
    _FakeAgent.responses = []
    yield
    _FakeAgent.calls = []
    _FakeAgent.responses = []


# ---------------------------------------------------------------------------
# Agent unit tests
# ---------------------------------------------------------------------------


def test_critic_returns_pass_on_clean_json() -> None:
    _FakeAgent.responses = [
        json.dumps(
            {
                "status": "PASS",
                "approved": True,
                "violations": [],
                "notes": "Plan looks good.",
                "rubric_version": "v1",
            }
        )
    ]
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("blog_plan_critic_agent.agent.Agent", _FakeAgent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="Brand spec text.",
            writing_guidelines="Writing guidelines text.",
            research_digest="Research.",
        )
    assert report.status == "PASS"
    assert report.approved is True
    assert report.violations == []
    assert len(_FakeAgent.calls) == 1


def test_critic_surfaces_violations_and_fails() -> None:
    _FakeAgent.responses = [
        json.dumps(
            {
                "status": "FAIL",
                "approved": False,
                "violations": [
                    {
                        "rule_id": "overarching_topic.stance_not_label",
                        "severity": "must_fix",
                        "section": "overall",
                        "evidence_quote": "A guide to caching",
                        "description": "Topic is a label, not a stance.",
                        "suggested_fix": "Rewrite as a stance.",
                    },
                    {
                        "rule_id": "section.key_points.specificity",
                        "severity": "must_fix",
                        "section": "Introduction",
                        "evidence_quote": "Discuss scaling",
                        "description": "Vague key point.",
                        "suggested_fix": "Replace with a specific claim.",
                    },
                ],
                "rubric_version": "v1",
            }
        )
    ]
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("blog_plan_critic_agent.agent.Agent", _FakeAgent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="Brand spec text.",
            writing_guidelines="Writing guidelines text.",
        )
    assert report.status == "FAIL"
    assert report.approved is False
    assert report.must_fix_count() == 2
    assert {v.rule_id for v in report.violations} == {
        "overarching_topic.stance_not_label",
        "section.key_points.specificity",
    }


def test_critic_approved_invariant_enforced() -> None:
    """approved must equal (status == PASS) regardless of what the LLM returned."""
    _FakeAgent.responses = [
        json.dumps(
            {
                "status": "FAIL",
                "approved": True,  # inconsistent with status; critic should fix
                "violations": [
                    {
                        "rule_id": "x",
                        "severity": "must_fix",
                        "description": "y",
                        "suggested_fix": "z",
                    }
                ],
                "rubric_version": "v1",
            }
        )
    ]
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("blog_plan_critic_agent.agent.Agent", _FakeAgent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
        )
    assert report.status == "FAIL"
    assert report.approved is False


def test_critic_parse_failure_falls_back_to_fail() -> None:
    _FakeAgent.responses = ["not json at all", "also not json"]
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("blog_plan_critic_agent.agent.Agent", _FakeAgent):
        report = critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
        )
    assert report.status == "FAIL"
    assert report.approved is False
    assert report.notes is not None
    assert "parseable JSON" in (report.notes or "")


def test_critic_persists_report_to_work_dir(tmp_path) -> None:
    _FakeAgent.responses = [
        json.dumps(
            {
                "status": "PASS",
                "approved": True,
                "violations": [],
                "rubric_version": "v1",
            }
        )
    ]
    critic = BlogPlanCriticAgent(llm_client=DummyLLMClient())
    with patch("blog_plan_critic_agent.agent.Agent", _FakeAgent):
        critic.run(
            plan=_minimal_plan(),
            brand_spec_prompt="b",
            writing_guidelines="g",
            work_dir=tmp_path,
            artifact_name="plan_critic_report_v1.json",
        )
    assert (tmp_path / "plan_critic_report_v1.json").exists()


# ---------------------------------------------------------------------------
# Refine-feedback formatting
# ---------------------------------------------------------------------------


def test_refine_feedback_lists_must_fix_first() -> None:
    report = PlanCriticReport(
        status="FAIL",
        approved=False,
        violations=[
            {
                "rule_id": "z.consider",
                "severity": "consider",
                "description": "consider item",
                "suggested_fix": "consider fix",
            },  # type: ignore[list-item]
            {
                "rule_id": "a.must_fix",
                "severity": "must_fix",
                "description": "must fix item",
                "suggested_fix": "must fix fix",
            },  # type: ignore[list-item]
            {
                "rule_id": "m.should_fix",
                "severity": "should_fix",
                "description": "should fix item",
                "suggested_fix": "should fix fix",
            },  # type: ignore[list-item]
        ],
    )
    feedback = build_refine_feedback_from_critic(report)
    must_idx = feedback.index("a.must_fix")
    should_idx = feedback.index("m.should_fix")
    consider_idx = feedback.index("z.consider")
    assert must_idx < should_idx < consider_idx
    assert "independent plan critic reviewed" in feedback


def test_refine_feedback_empty_when_approved() -> None:
    report = PlanCriticReport(status="PASS", approved=True, violations=[])
    assert "no refinement needed" in build_refine_feedback_from_critic(report)


# ---------------------------------------------------------------------------
# Integration: BlogPlanningAgent with the critic
# ---------------------------------------------------------------------------


def test_planning_agent_integrates_critic_and_persists_reports(tmp_path) -> None:
    """When a critic is attached, the planner gates on critic approval and writes artifacts."""
    llm = DummyLLMClient()
    critic = BlogPlanCriticAgent(llm_client=llm)
    agent = BlogPlanningAgent(
        llm,
        plan_critic=critic,
        brand_spec_prompt="Brand: tests.",
        writing_guidelines="Guidelines: keep it short.",
    )
    inp = PlanningInput(
        brief="Test brief about observability.",
        research_digest="## Sources\n- Source one: summary.",
        length_policy_context=_policy_standard().length_guidance,
    )
    result = agent.run(inp, length_policy=_policy_standard(), work_dir=tmp_path)
    assert result.content_plan.requirements_analysis.plan_acceptable is True
    # Critic report is attached to the result and persisted.
    assert result.plan_critic_report is not None
    assert result.plan_critic_report["status"] == "PASS"
    assert result.plan_critic_report["approved"] is True
    assert (tmp_path / "plan_critic_report_v1.json").exists()


def test_planning_agent_without_critic_keeps_legacy_behaviour() -> None:
    """When no critic is attached, the planner's self-eval is authoritative."""
    llm = DummyLLMClient()
    agent = BlogPlanningAgent(llm)
    inp = PlanningInput(
        brief="Test brief about observability.",
        research_digest="## Sources\n- Source one: summary.",
        length_policy_context=_policy_standard().length_guidance,
    )
    result = agent.run(inp, length_policy=_policy_standard())
    assert result.plan_critic_report is None
