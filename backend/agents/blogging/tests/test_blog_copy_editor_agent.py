"""Tests for the blog copy editor agent."""

import pytest

from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput, CopyEditorOutput
from blog_research_agent.llm import DummyLLMClient


def test_blog_copy_editor_agent_run() -> None:
    """BlogCopyEditorAgent returns summary and feedback_items."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(llm_client=llm)

    copy_editor_input = CopyEditorInput(
        draft="# Test Post\n\nThis is a draft with an em dash—here.",
        audience="CTOs",
        tone_or_purpose="technical",
    )

    result = agent.run(copy_editor_input)

    assert isinstance(result, CopyEditorOutput)
    assert result.summary
    assert isinstance(result.feedback_items, list)
    # DummyLLMClient returns at least one feedback item
    assert len(result.feedback_items) >= 1
    item = result.feedback_items[0]
    assert item.category
    assert item.severity in ("must_fix", "should_fix", "consider")
    assert item.issue


def test_blog_copy_editor_agent_empty_draft() -> None:
    """BlogCopyEditorAgent returns minimal feedback for empty draft."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(llm_client=llm)

    result = agent.run(CopyEditorInput(draft=""))

    assert result.summary
    assert len(result.feedback_items) == 0
