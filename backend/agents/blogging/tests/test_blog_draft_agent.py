"""Tests for the blog draft agent."""

import pytest

from blog_draft_agent import BlogDraftAgent, DraftInput, DraftOutput
from blog_research_agent.llm import DummyLLMClient


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
    """BlogDraftAgent accepts optional style_guide in input."""
    llm = DummyLLMClient()
    agent = BlogDraftAgent(llm_client=llm)

    draft_input = DraftInput(
        research_document="Research here.",
        outline="Outline here.",
        style_guide="Write like a mentor. Short sentences. No em dashes.",
    )

    result = agent.run(draft_input)
    assert result.draft
