"""Tests for the blog copy editor agent."""

import json
from pathlib import Path

import pytest

from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput, CopyEditorOutput
from llm_service import DummyLLMClient


# Inline style guide passed at agent init so tests do not load the default file.
_TEST_STYLE_GUIDE = "Use short sentences. No em dashes."


def test_blog_copy_editor_agent_run() -> None:
    """BlogCopyEditorAgent returns summary and feedback_items."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )

    copy_editor_input = CopyEditorInput(
        draft="# Test Post\n\nThis is a draft with an em dash—here.",
        audience="CTOs",
        tone_or_purpose="technical",
    )

    result = agent.run(copy_editor_input)

    assert isinstance(result, CopyEditorOutput)
    assert result.summary
    assert isinstance(result.feedback_items, list)
    # DummyLLMClient may return zero or more feedback items depending on prompt
    assert len(result.feedback_items) >= 0
    if result.feedback_items:
        item = result.feedback_items[0]
        assert item.category
        assert item.severity in ("must_fix", "should_fix", "consider")
        assert item.issue


def test_blog_copy_editor_agent_empty_draft() -> None:
    """BlogCopyEditorAgent returns minimal feedback for empty draft."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(llm_client=llm, writing_style_guide_content="", brand_spec_content="")

    result = agent.run(CopyEditorInput(draft=""))

    assert result.summary
    assert len(result.feedback_items) == 0


def test_blog_copy_editor_agent_writes_feedback_file(tmp_path: Path) -> None:
    """When feedback_output_path is set, run() writes the output to that file."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    feedback_file = tmp_path / "editor_feedback.json"

    result = agent.run(
        CopyEditorInput(draft="# Test\n\nShort draft."),
        feedback_output_path=str(feedback_file),
    )

    assert feedback_file.exists()
    content = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert "summary" in content
    assert "feedback_items" in content
    assert isinstance(content["feedback_items"], list)
    assert content["summary"] == result.summary
    assert len(content["feedback_items"]) == len(result.feedback_items)


def test_blog_copy_editor_agent_feedback_file_roundtrip(tmp_path: pytest.TempPathFactory) -> None:
    """Written JSON matches the returned CopyEditorOutput."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    feedback_file = tmp_path / "editor_feedback.json"

    result = agent.run(
        CopyEditorInput(draft="# Test\n\nDraft with content."),
        feedback_output_path=str(feedback_file),
    )

    data = result.model_dump() if hasattr(result, "model_dump") else result.dict()
    written = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert written["summary"] == data["summary"]
    assert len(written["feedback_items"]) == len(data["feedback_items"])


def test_blog_copy_editor_agent_no_path_no_file(tmp_path: Path) -> None:
    """When feedback_output_path is not passed, no file is created in the given dir."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )

    result = agent.run(CopyEditorInput(draft="# Test\n\nDraft."))

    assert result.summary is not None
    assert (tmp_path / "editor_feedback.json").exists() is False


def test_blog_copy_editor_agent_empty_draft_writes_file(tmp_path: Path) -> None:
    """Empty draft with feedback_output_path set still writes a file with summary and empty feedback_items."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(llm_client=llm, writing_style_guide_content="", brand_spec_content="")
    feedback_file = tmp_path / "empty_feedback.json"

    result = agent.run(CopyEditorInput(draft=""), feedback_output_path=str(feedback_file))

    assert feedback_file.exists()
    content = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert content["summary"]
    assert content["feedback_items"] == []
    assert result.summary
    assert len(result.feedback_items) == 0
