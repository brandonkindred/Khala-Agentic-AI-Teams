"""Tests for BlogPlanningAgent and ContentPlan validation."""

from __future__ import annotations

from blog_planning_agent import BlogPlanningAgent
from blog_planning_agent.agent import _post_validate
from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    PlanningInput,
    RequirementsAnalysis,
    TitleCandidate,
    section_count_bounds_for_profile,
)
from shared.content_profile import ContentProfile, LengthPolicy, resolve_length_policy

from llm_service import DummyLLMClient


def _policy_standard() -> LengthPolicy:
    return resolve_length_policy(content_profile=ContentProfile.standard_article)


def test_content_plan_json_roundtrip() -> None:
    plan = ContentPlan(
        overarching_topic="Topic",
        narrative_flow="A then B.",
        sections=[
            ContentPlanSection(title="A", coverage_description="Do A", order=0, gap_flag=True),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.6)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True,
            scope_feasible=True,
            research_gaps=["gap1"],
        ),
    )
    data = plan.model_dump(mode="json")
    restored = ContentPlan.model_validate(data)
    assert restored.overarching_topic == plan.overarching_topic
    assert restored.sections[0].gap_flag is True


def test_section_count_bounds() -> None:
    lo, hi = section_count_bounds_for_profile("standard_article")
    assert lo <= hi


def test_planning_agent_dummy_llm_produces_acceptable_plan() -> None:
    llm = DummyLLMClient()
    agent = BlogPlanningAgent(llm)
    inp = PlanningInput(
        brief="Test brief about observability.",
        research_digest="## Sources\n- Source one: summary.",
        length_policy_context=_policy_standard().length_guidance,
    )
    result = agent.run(inp, length_policy=_policy_standard())
    assert result.content_plan.requirements_analysis.plan_acceptable
    assert result.content_plan.requirements_analysis.scope_feasible
    assert result.planning_iterations_used >= 1


def test_post_validate_flags_section_count() -> None:
    """Too many sections for profile sets plan_acceptable False."""
    sections = [
        ContentPlanSection(title=f"S{i}", coverage_description="x", order=i) for i in range(15)
    ]
    plan = ContentPlan(
        overarching_topic="T",
        narrative_flow="n",
        sections=sections,
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True,
            scope_feasible=True,
            research_gaps=[],
        ),
    )
    out = _post_validate(plan, _policy_standard())
    assert out.requirements_analysis.plan_acceptable is False
    assert any("outside expected range" in g for g in out.requirements_analysis.gaps)
