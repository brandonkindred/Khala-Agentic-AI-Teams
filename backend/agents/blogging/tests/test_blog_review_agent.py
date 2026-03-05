"""Tests for the blog review agent."""

import pytest

from blog_review_agent import BlogReviewAgent, BlogReviewInput
from blog_research_agent.models import ResearchReference
from blog_research_agent.llm import DummyLLMClient


def test_blog_review_agent_run() -> None:
    """BlogReviewAgent returns 5 title choices and a non-empty outline."""
    llm = DummyLLMClient()
    agent = BlogReviewAgent(llm_client=llm)

    ref = ResearchReference(
        title="Test Source",
        url="https://example.com",
        summary="A test summary.",
        key_points=["Point A", "Point B"],
    )
    review_input = BlogReviewInput(
        brief="LLM observability for enterprises",
        audience="CTOs",
        tone_or_purpose="technical deep-dive",
        references=[ref],
    )

    result = agent.run(review_input)

    assert len(result.title_choices) == 5
    for tc in result.title_choices:
        assert tc.title
        assert 0.0 <= tc.probability_of_success <= 1.0
    assert result.outline
    assert "Introduction" in result.outline or "Outline" in result.outline
