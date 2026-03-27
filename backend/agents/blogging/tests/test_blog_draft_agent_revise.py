"""Tests for BlogDraftAgent.revise (plan-first batch feedback processing)."""

from __future__ import annotations

from unittest.mock import MagicMock

from blog_copy_editor_agent.models import FeedbackItem
from blog_draft_agent import BlogDraftAgent, ReviseDraftInput
from blog_draft_agent.prompts import WRITING_SYSTEM_PROMPT
from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    RequirementsAnalysis,
    TitleCandidate,
)


def _minimal_plan() -> ContentPlan:
    return ContentPlan(
        overarching_topic="Test topic",
        narrative_flow="Intro, main, wrap.",
        sections=[
            ContentPlanSection(title="Intro", coverage_description="Hook", order=0),
        ],
        title_candidates=[TitleCandidate(title="T1", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True,
            scope_feasible=True,
            research_gaps=[],
        ),
    )


def test_revise_generates_plan_then_applies_all_feedback() -> None:
    llm = MagicMock()
    llm.complete.side_effect = [
        "1) Priority order\n2) Section updates\n3) Citation fixes",
        '{"draft": 0}\n---DRAFT---\n# Revised title\n\nBody here.\n',
    ]
    agent = BlogDraftAgent(
        llm_client=llm,
        writing_style_guide_content="Use short paragraphs.",
        brand_spec_content="Brand voice: practical and direct.",
    )
    items = [
        FeedbackItem(
            category="style",
            severity="must_fix",
            location="intro",
            issue="Opening is weak.",
            suggestion="Add a concrete hook.",
        ),
        FeedbackItem(
            category="structure",
            severity="should_fix",
            issue="Section two drags.",
            suggestion="Tighten examples.",
        ),
    ]
    inp = ReviseDraftInput(
        draft="# Original\n\nOld body.\n",
        feedback_items=items,
        content_plan=_minimal_plan(),
    )
    out = agent.revise(inp)

    # Two LLM calls: plan + apply
    assert llm.complete.call_count == 2
    llm.complete_json.assert_not_called()

    # First call generates the revision plan from all feedback items
    first_prompt = llm.complete.call_args_list[0][0][0]
    assert "Create a revision plan for this draft." in first_prompt
    assert "Opening is weak." in first_prompt
    assert "Section two drags." in first_prompt

    # Second call applies the planned batch revision
    second_prompt = llm.complete.call_args_list[1][0][0]
    assert "REVISION PLAN (execute this plan before writing):" in second_prompt
    assert "COPY EDITOR FEEDBACK (apply every numbered item below):" in second_prompt
    assert "Section two drags." in second_prompt

    # Both calls use the writing system prompt with plan-then-apply temperatures
    assert llm.complete.call_args_list[0].kwargs.get("system_prompt") == WRITING_SYSTEM_PROMPT
    assert llm.complete.call_args_list[0].kwargs.get("temperature") == 0.1
    assert llm.complete.call_args_list[1].kwargs.get("system_prompt") == WRITING_SYSTEM_PROMPT
    assert llm.complete.call_args_list[1].kwargs.get("temperature") == 0.2

    assert "# Revised title" in out.draft
    assert "Body here." in out.draft
