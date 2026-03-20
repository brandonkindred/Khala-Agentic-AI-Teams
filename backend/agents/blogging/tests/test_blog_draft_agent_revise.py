"""Tests for BlogDraftAgent.revise (batched copy-editor feedback)."""

from __future__ import annotations

from unittest.mock import MagicMock

from blog_copy_editor_agent.models import FeedbackItem
from blog_draft_agent import BlogDraftAgent, ReviseDraftInput
from blog_draft_agent.prompts import REVISE_DRAFT_PROMPT
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


def test_revise_batches_all_feedback_in_one_llm_call() -> None:
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

    assert llm.complete.call_count == 1
    llm.complete_json.assert_not_called()

    call_kw = llm.complete.call_args.kwargs
    assert call_kw.get("system_prompt") == REVISE_DRAFT_PROMPT
    assert call_kw.get("temperature") == 0.2

    prompt = llm.complete.call_args[0][0]
    assert "COPY EDITOR FEEDBACK (apply every numbered item below):" in prompt
    assert "1. [must_fix] style [intro]: Opening is weak." in prompt
    assert "Suggestion: Add a concrete hook." in prompt
    assert "2. [should_fix] structure:" in prompt
    assert "Section two drags." in prompt
    assert "CONTENT PLAN (preserve section intent and narrative flow):" in prompt

    assert "# Revised title" in out.draft
    assert "Body here." in out.draft
