"""Tests for the blog draft agent."""

import re

import pytest
from blog_draft_agent import BlogDraftAgent, DraftInput, DraftOutput
from blog_research_agent.models import ResearchReference
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
            ContentPlanSection(title="Main", coverage_description="Body", order=1),
        ],
        title_candidates=[TitleCandidate(title="T1", probability_of_success=0.5)],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True,
            scope_feasible=True,
            research_gaps=[],
        ),
    )


class _PromptCapturingLLM(DummyLLMClient):
    """Dummy LLM that records the last prompt passed to complete() for tests."""

    def __init__(self) -> None:
        super().__init__()
        self.last_prompt: str = ""
        self.last_complete_json_prompt: str = ""

    def complete(self, prompt: str, **kwargs: object) -> str:
        self.last_prompt = prompt
        return '{"draft": 0}\n---DRAFT---\n# Draft\n\nPlaceholder draft content.'

    def complete_json(self, prompt: str, **kwargs: object) -> dict:
        self.last_complete_json_prompt = prompt
        return {"draft": "# Draft\n\nPlaceholder."}


def test_draft_input_requires_research_source() -> None:
    """DraftInput raises when both research_document and research_references are empty."""
    p = _minimal_plan()
    with pytest.raises(
        ValueError, match="either research_document or non-empty research_references"
    ):
        DraftInput(
            research_document=None,
            research_references=None,
            content_plan=p,
        )
    with pytest.raises(
        ValueError, match="either research_document or non-empty research_references"
    ):
        DraftInput(
            research_document="",
            research_references=[],
            content_plan=p,
        )


def test_golden_draft_h2_headings_match_content_plan_sections() -> None:
    """Regression: Markdown H2 headings follow planned section titles (order)."""
    plan = _minimal_plan()

    class H2DraftLLM(DummyLLMClient):
        def complete(self, prompt, **kwargs):  # type: ignore[no-untyped-def]
            self._request_count += 1
            body = "\n\n".join(
                f"## {s.title}\n\nBody for {s.title}."
                for s in sorted(plan.sections, key=lambda x: x.order)
            )
            return '{"draft": 0}\n---DRAFT---\n# Post title\n\n' + body

    agent = BlogDraftAgent(llm_client=H2DraftLLM())
    out = agent.run(DraftInput(research_document="Compiled research text.", content_plan=plan))
    h2s = re.findall(r"^## (.+)$", out.draft, re.MULTILINE)
    expected = [s.title for s in sorted(plan.sections, key=lambda x: x.order)]
    assert h2s == expected


def test_blog_draft_agent_run() -> None:
    """BlogDraftAgent returns a non-empty draft from research + content plan."""
    llm = DummyLLMClient()
    agent = BlogDraftAgent(llm_client=llm)

    draft_input = DraftInput(
        research_document="Compiled research: Source 1 summary. Source 2 key points.",
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)

    assert isinstance(result, DraftOutput)
    assert result.draft
    assert (
        "draft" in result.draft.lower()
        or "introduction" in result.draft.lower()
        or "placeholder" in result.draft.lower()
    )


def test_blog_draft_agent_with_style_guide() -> None:
    """BlogDraftAgent uses writing_style_guide_content passed at init."""
    llm = DummyLLMClient()
    agent = BlogDraftAgent(
        llm_client=llm,
        writing_style_guide_content="Write like a mentor. Clear, natural-length sentences. No em dashes.",
        brand_spec_content="",
    )

    draft_input = DraftInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)
    assert result.draft


def test_blog_draft_agent_run_with_research_references() -> None:
    """BlogDraftAgent runs parallel extraction then draft when research_references is provided."""
    llm = DummyLLMClient()
    agent = BlogDraftAgent(llm_client=llm)

    refs = [
        ResearchReference(
            title="Source One",
            url="https://example.com/one",
            summary="Summary of first source.",
            key_points=["Point A", "Point B"],
        ),
        ResearchReference(
            title="Source Two",
            url="https://example.com/two",
            summary="Summary of second source.",
        ),
    ]
    draft_input = DraftInput(
        research_document=None,
        research_references=refs,
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)

    assert isinstance(result, DraftOutput)
    assert result.draft
    assert (
        "draft" in result.draft.lower()
        or "placeholder" in result.draft.lower()
        or "introduction" in result.draft.lower()
    )


def test_draft_prompt_includes_provided_brand_spec() -> None:
    """When brand_spec_content is provided, the draft prompt includes it in the BRAND AND STYLE section."""
    llm = _PromptCapturingLLM()
    agent = BlogDraftAgent(
        llm_client=llm,
        writing_style_guide_content="",
        brand_spec_content="MyBrand: Test brand. Voice: friendly and clear.",
    )
    draft_input = DraftInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )
    agent.run(draft_input)
    assert "MyBrand: Test brand." in llm.last_prompt
    assert "BRAND AND STYLE" in llm.last_prompt


def test_outline_for_prompt_includes_section_titles() -> None:
    """outline_for_prompt flattens the content plan for LLM consumption."""
    inp = DraftInput(
        research_document="R",
        content_plan=_minimal_plan(),
    )
    text = inp.outline_for_prompt()
    assert "Test topic" in text
    assert "Intro" in text
    assert "Main" in text


def test_draft_prompt_includes_fallback_when_no_brand_spec() -> None:
    """When brand_spec_content is empty, the draft prompt includes the fallback line and no hardcoded primer."""
    llm = _PromptCapturingLLM()
    agent = BlogDraftAgent(
        llm_client=llm,
        writing_style_guide_content="",
        brand_spec_content="",
    )
    draft_input = DraftInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )
    agent.run(draft_input)
    assert "No brand specification was provided. Follow the style guide below." in llm.last_prompt
    assert "BRAND AND STYLE" in llm.last_prompt
    assert "You are writing in a specific brand voice" not in llm.last_prompt
