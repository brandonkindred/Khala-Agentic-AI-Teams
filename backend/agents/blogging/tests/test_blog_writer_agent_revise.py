"""Tests for BlogWriterAgent.revise (plan-first batch feedback processing)."""

from __future__ import annotations

from typing import Any

from blog_copy_editor_agent.models import FeedbackItem
from blog_writer_agent import BlogWriterAgent, ReviseWriterInput
from shared.content_plan import (
    ContentPlan,
    ContentPlanSection,
    RequirementsAnalysis,
    TitleCandidate,
)

from llm_service import DummyLLMClient


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


class _ReviseTrackingLLM(DummyLLMClient):
    """A DummyLLMClient subclass that tracks calls and returns canned responses for the revise flow.

    The first call (revision plan) returns a structured JSON plan.
    Subsequent calls (apply revision) return a hybrid draft format.
    """

    def __init__(self) -> None:
        super().__init__()
        self._call_index = 0
        self.captured_prompts: list[str] = []

    def complete_json(self, prompt: str, **kwargs: Any) -> dict:
        self._request_count += 1
        self._call_index += 1
        self.captured_prompts.append(prompt)
        if self._call_index == 1:
            # First call: revision plan
            return {
                "summary": "Fix opening hook and tighten section two.",
                "changes": [
                    {"section": "intro", "feedback_ids": [1], "action": "rewrite", "rationale": "Weak opening."},
                    {"section": "section two", "feedback_ids": [2], "action": "rephrase", "rationale": "Drags."},
                ],
                "risks": [],
            }
        # Subsequent calls: return draft
        return {"draft": "# Revised title\n\nBody here."}


def test_revise_generates_plan_then_applies_all_feedback() -> None:
    llm = _ReviseTrackingLLM()
    agent = BlogWriterAgent(
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
    inp = ReviseWriterInput(
        draft="# Original\n\nOld body.\n",
        feedback_items=items,
        content_plan=_minimal_plan(),
    )
    out = agent.revise(inp)

    # At least 2 calls: one for the revision plan, one to apply it
    assert len(llm.captured_prompts) >= 2

    # First call (plan) includes all feedback items
    plan_prompt = llm.captured_prompts[0]
    assert "Opening is weak." in plan_prompt
    assert "Section two drags." in plan_prompt

    # Second call (apply) includes both the revision plan and the feedback
    apply_prompt = llm.captured_prompts[1]
    assert "REVISION PLAN (execute this plan before writing):" in apply_prompt
    assert "COPY EDITOR FEEDBACK (apply every numbered item below):" in apply_prompt
    assert "Section two drags." in apply_prompt

    assert "# Revised title" in out.draft
    assert "Body here." in out.draft
