"""Tests for ContentPlan helpers."""

from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    RequirementsAnalysis,
    TitleCandidate,
    content_plan_summary_text,
)


def test_requirements_analysis_json_round_trip() -> None:
    ra = RequirementsAnalysis(
        plan_acceptable=True,
        scope_feasible=False,
        research_gaps=["needs source on X"],
        fits_profile=False,
        gaps=["structure"],
        risks=["tone"],
        suggested_format_change="technical_deep_dive",
    )
    ra2 = RequirementsAnalysis.model_validate(ra.model_dump(mode="json"))
    assert ra2 == ra


def _minimal_plan(**kwargs) -> ContentPlan:
    ra = RequirementsAnalysis(
        plan_acceptable=True,
        scope_feasible=True,
        research_gaps=[],
    )
    defaults = dict(
        overarching_topic="Topic",
        narrative_flow="Start here, then there, then conclude.",
        sections=[
            ContentPlanSection(
                title="One",
                coverage_description="A",
                order=0,
            ),
        ],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.9)],
        requirements_analysis=ra,
    )
    defaults.update(kwargs)
    return ContentPlan(**defaults)


def test_content_plan_summary_text_concatenates_and_truncates() -> None:
    plan = _minimal_plan()
    s = content_plan_summary_text(plan, max_chars=500)
    assert "Topic" in s
    assert "Start here" in s

    long_flow = "x" * 2000
    plan2 = _minimal_plan(narrative_flow=long_flow)
    s2 = content_plan_summary_text(plan2, max_chars=100)
    assert len(s2) <= 100
    assert s2.endswith("...")
