"""Tests for the blog draft agent."""

import pytest

from blog_draft_agent import BlogDraftAgent, DraftInput, DraftOutput
from blog_research_agent.models import ResearchReference
from llm_service import DummyLLMClient


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
