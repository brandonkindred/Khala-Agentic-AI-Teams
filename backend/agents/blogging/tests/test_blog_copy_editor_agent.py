"""Tests for the blog copy editor agent."""

import json
from pathlib import Path

import pytest
from blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorInput, CopyEditorOutput

from llm_service import DummyLLMClient

# Inline style guide passed at agent init so tests do not load the default file.
_TEST_STYLE_GUIDE = "Clear, conversational prose at ~8th grade. No em dashes."


def _draft_n_words(n: int) -> str:
    """Whitespace-separated tokens so len(draft.split()) == n."""
    return " ".join(["word"] * n)


def _structure_length_items(feedback_items: list) -> list:
    return [
        item
        for item in feedback_items
        if item.category == "structure"
        and item.location == "entire draft"
        and item.issue
        and "words" in item.issue.lower()
    ]


def test_length_injection_skips_when_within_soft_ceiling() -> None:
    """1134 words vs ~1000 target must not inject should_fix when soft_max allows it (e.g. 1300)."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    result = agent.run(
        CopyEditorInput(
            draft=_draft_n_words(1134),
            target_word_count=1000,
            soft_min_words=750,
            soft_max_words=1300,
        )
    )
    assert _structure_length_items(result.feedback_items) == []


def test_length_injection_must_fix_past_soft_ceiling() -> None:
    """Above soft_max, ratio vs target still triggers programmatic length feedback."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    result = agent.run(
        CopyEditorInput(
            draft=_draft_n_words(1400),
            target_word_count=1000,
            soft_min_words=750,
            soft_max_words=1300,
        )
    )
    length_items = _structure_length_items(result.feedback_items)
    assert len(length_items) >= 1
    assert length_items[0].severity == "must_fix"


def test_length_injection_soft_max_none_uses_ratio_only() -> None:
    """Without soft_max, modest overrun vs target still gets should_fix (legacy behavior)."""
    llm = DummyLLMClient()
    agent = BlogCopyEditorAgent(
        llm_client=llm,
        writing_style_guide_content=_TEST_STYLE_GUIDE,
        brand_spec_content="",
    )
    result = agent.run(
        CopyEditorInput(
            draft=_draft_n_words(1111),
            target_word_count=1000,
            soft_min_words=None,
            soft_max_words=None,
        )
    )
    length_items = _structure_length_items(result.feedback_items)
    assert len(length_items) == 1
    assert length_items[0].severity == "should_fix"


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
    agent = BlogCopyEditorAgent(
        llm_client=llm, writing_style_guide_content="", brand_spec_content=""
    )

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
    agent = BlogCopyEditorAgent(
        llm_client=llm, writing_style_guide_content="", brand_spec_content=""
    )
    feedback_file = tmp_path / "empty_feedback.json"

    result = agent.run(CopyEditorInput(draft=""), feedback_output_path=str(feedback_file))

    assert feedback_file.exists()
    content = json.loads(feedback_file.read_text(encoding="utf-8"))
    assert content["summary"]
    assert content["feedback_items"] == []
    assert result.summary
    assert len(result.feedback_items) == 0
