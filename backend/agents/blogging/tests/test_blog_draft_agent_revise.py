"""Tests for BlogDraftAgent.revise (one-item-at-a-time feedback processing)."""

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


def test_revise_processes_each_feedback_item_separately() -> None:
    llm = MagicMock()
    llm.complete.return_value = '{"draft": 0}\n---DRAFT---\n# Revised title\n\nBody here.\n'
    agent = BlogDraftAgent(llm_client=llm, writing_style_guide_content="Use short paragraphs.")
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

    # One LLM call per feedback item
    assert llm.complete.call_count == 2
    llm.complete_json.assert_not_called()

    # First call addresses item 1
    first_prompt = llm.complete.call_args_list[0][0][0]
    assert "1/2" in first_prompt
    assert "Opening is weak." in first_prompt
    assert "FEEDBACK TO ADDRESS" in first_prompt

    # Second call addresses item 2
    second_prompt = llm.complete.call_args_list[1][0][0]
    assert "2/2" in second_prompt
    assert "Section two drags." in second_prompt

    # Both calls use the writing system prompt
    for call in llm.complete.call_args_list:
        assert call.kwargs.get("system_prompt") == WRITING_SYSTEM_PROMPT
        assert call.kwargs.get("temperature") == 0.2

    assert "# Revised title" in out.draft
    assert "Body here." in out.draft
