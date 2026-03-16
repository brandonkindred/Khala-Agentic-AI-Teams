"""Tests for the blog draft agent."""

import pytest

from blog_draft_agent import BlogDraftAgent, DraftInput, DraftOutput
from blog_research_agent.models import ResearchReference
from llm_service import DummyLLMClient


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
    with pytest.raises(ValueError, match="either research_document or non-empty research_references"):
        DraftInput(
            research_document=None,
            research_references=None,
            outline="# Intro\n# Main",
        )
    with pytest.raises(ValueError, match="either research_document or non-empty research_references"):
        DraftInput(
            research_document="",
            research_references=[],
            outline="# Intro\n# Main",
        )


def test_blog_draft_agent_run() -> None:
    """BlogDraftAgent returns a non-empty draft from research + outline."""
    llm = DummyLLMClient()
    agent = BlogDraftAgent(llm_client=llm)

    draft_input = DraftInput(
        research_document="Compiled research: Source 1 summary. Source 2 key points.",
        outline="# Intro\n# Main\n# Wrap up",
    )

    result = agent.run(draft_input)

    assert isinstance(result, DraftOutput)
    assert result.draft
    assert "draft" in result.draft.lower() or "introduction" in result.draft.lower() or "placeholder" in result.draft.lower()


def test_blog_draft_agent_with_style_guide() -> None:
    """BlogDraftAgent uses writing_style_guide_content passed at init."""
    llm = DummyLLMClient()
    agent = BlogDraftAgent(
        llm_client=llm,
        writing_style_guide_content="Write like a mentor. Short sentences. No em dashes.",
        brand_spec_content="",
    )

    draft_input = DraftInput(
        research_document="Research here.",
        outline="Outline here.",
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
        outline="# Intro\n# Main\n# Wrap up",
    )

    result = agent.run(draft_input)

    assert isinstance(result, DraftOutput)
    assert result.draft
    assert "draft" in result.draft.lower() or "placeholder" in result.draft.lower() or "introduction" in result.draft.lower()


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
        outline="# Intro\n# Main",
    )
    agent.run(draft_input)
    assert "MyBrand: Test brand." in llm.last_prompt
    assert "BRAND AND STYLE" in llm.last_prompt


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
        outline="# Intro\n# Main",
    )
    agent.run(draft_input)
    assert "No brand specification was provided. Follow the style guide below." in llm.last_prompt
    assert "BRAND AND STYLE" in llm.last_prompt
    # Regression: no hardcoded BRAND_AND_STYLE_PRIMER (old primer started with this line)
    assert "You are writing in a specific brand voice" not in llm.last_prompt
