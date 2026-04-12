"""Tests for the blog writer agent."""

import re

import pytest
from blog_research_agent.models import ResearchReference
from blog_writer_agent import BlogWriterAgent, WriterInput, WriterOutput
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
    """Dummy LLM that records all prompts for tests.

    Since the blogging agents now use ``strands.Agent(model=llm)`` which calls
    ``stream()`` -> ``complete_json()``, prompt capture happens in
    ``complete_json`` (called by the inherited ``DummyLLMClient.stream``).
    """

    def __init__(self) -> None:
        super().__init__()
        self.last_prompt: str = ""
        self.all_prompts: list[str] = []
        self.last_complete_json_prompt: str = ""

    def complete_json(self, prompt: str, **kwargs: object) -> dict:
        self.last_prompt = prompt
        self.all_prompts.append(prompt)
        self.last_complete_json_prompt = prompt
        lowered = prompt.lower() if isinstance(prompt, str) else ""
        # Self-review prompt: return empty issues list
        if "review this draft" in lowered:
            return {"issues": []}
        # Deterministic fix prompt
        if "fix only these" in lowered:
            return {"draft": "# Draft\n\nPlaceholder draft content."}
        return {"draft": "# Draft\n\nPlaceholder draft content."}


def test_writer_input_requires_content_plan() -> None:
    """WriterInput raises when content_plan is missing."""
    with pytest.raises(ValueError, match="content_plan"):
        WriterInput(
            content_plan=None,  # type: ignore[arg-type]
        )


def test_golden_draft_h2_headings_match_content_plan_sections() -> None:
    """Regression: Markdown H2 headings follow planned section titles (order)."""
    plan = _minimal_plan()

    class H2DraftLLM(DummyLLMClient):
        """Override complete_json so DummyLLMClient.stream() returns a draft
        whose H2 headings match the content plan sections."""

        def complete_json(self, prompt, **kwargs):  # type: ignore[no-untyped-def]
            self._request_count += 1
            lowered = prompt.lower() if isinstance(prompt, str) else ""
            # Self-review prompt: return empty issues list
            if "review this draft" in lowered:
                return {"issues": []}
            # Deterministic fix prompt: pass through
            if "fix only these" in lowered:
                body = "\n\n".join(
                    f"## {s.title}\n\nBody for {s.title}."
                    for s in sorted(plan.sections, key=lambda x: x.order)
                )
                return {"draft": "# Post title\n\n" + body}
            # Default: return draft with planned H2 headings
            body = "\n\n".join(
                f"## {s.title}\n\nBody for {s.title}."
                for s in sorted(plan.sections, key=lambda x: x.order)
            )
            return {"draft": "# Post title\n\n" + body}

    agent = BlogWriterAgent(
        llm_client=H2DraftLLM(),
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )
    out = agent.run(WriterInput(research_document="Compiled research text.", content_plan=plan))
    h2s = re.findall(r"^## (.+)$", out.draft, re.MULTILINE)
    expected = [s.title for s in sorted(plan.sections, key=lambda x: x.order)]
    assert h2s == expected


def test_blog_writer_agent_run() -> None:
    """BlogWriterAgent returns a non-empty draft from research + content plan."""
    llm = DummyLLMClient()
    agent = BlogWriterAgent(
        llm_client=llm,
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )

    draft_input = WriterInput(
        research_document="Compiled research: Source 1 summary. Source 2 key points.",
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)

    assert isinstance(result, WriterOutput)
    assert result.draft
    assert (
        "draft" in result.draft.lower()
        or "introduction" in result.draft.lower()
        or "placeholder" in result.draft.lower()
    )


def test_blog_writer_agent_with_style_guide() -> None:
    """BlogWriterAgent uses writing_style_guide_content passed at init."""
    llm = DummyLLMClient()
    agent = BlogWriterAgent(
        llm_client=llm,
        writing_style_guide_content="Write like a mentor. Clear, natural-length sentences. No em dashes.",
        brand_spec_content="Brand voice: practical and clear.",
    )

    draft_input = WriterInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)
    assert result.draft


def test_blog_writer_agent_run_with_research_references() -> None:
    """BlogWriterAgent runs parallel extraction then draft when research_references is provided."""
    llm = DummyLLMClient()
    agent = BlogWriterAgent(
        llm_client=llm,
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )

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
    draft_input = WriterInput(
        research_document=None,
        research_references=refs,
        content_plan=_minimal_plan(),
    )

    result = agent.run(draft_input)

    assert isinstance(result, WriterOutput)
    assert result.draft
    assert (
        "draft" in result.draft.lower()
        or "placeholder" in result.draft.lower()
        or "introduction" in result.draft.lower()
    )


def test_draft_prompt_includes_provided_brand_spec() -> None:
    """When brand_spec_content is provided, the draft prompt includes it in the BRAND AND STYLE section."""
    llm = _PromptCapturingLLM()
    agent = BlogWriterAgent(
        llm_client=llm,
        writing_style_guide_content="Use concise, natural sentences.",
        brand_spec_content="MyBrand: Test brand. Voice: friendly and clear.",
    )
    draft_input = WriterInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )
    agent.run(draft_input)
    # First prompt is the draft generation; subsequent ones are self-review
    draft_prompt = llm.all_prompts[0]
    assert "MyBrand: Test brand." in draft_prompt
    assert "BRAND AND STYLE" in draft_prompt


def test_outline_for_prompt_includes_section_titles() -> None:
    """outline_for_prompt flattens the content plan for LLM consumption."""
    inp = WriterInput(
        research_document="R",
        content_plan=_minimal_plan(),
    )
    text = inp.outline_for_prompt()
    assert "Test topic" in text
    assert "Intro" in text
    assert "Main" in text


def test_draft_run_requires_both_guidelines() -> None:
    """Draft agent rejects run() when brand/writing guidelines are missing."""
    llm = DummyLLMClient()
    agent = BlogWriterAgent(
        llm_client=llm,
        writing_style_guide_content="",
        brand_spec_content="",
    )
    draft_input = WriterInput(
        research_document="Research here.",
        content_plan=_minimal_plan(),
    )
    with pytest.raises(ValueError, match="requires both brand and writing guidelines"):
        agent.run(draft_input)
