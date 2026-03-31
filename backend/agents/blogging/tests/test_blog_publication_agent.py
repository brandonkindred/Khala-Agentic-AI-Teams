"""Tests for the blog publication agent."""

import pytest
from blog_publication_agent import (
    BlogPublicationAgent,
    SubmitDraftInput,
)

from llm_service import DummyLLMClient


@pytest.fixture
def temp_blog_root(tmp_path):
    return tmp_path / "blog_posts"


@pytest.fixture
def agent(temp_blog_root):
    llm = DummyLLMClient()
    return BlogPublicationAgent(
        llm_client=llm, blog_posts_root=temp_blog_root, max_revision_loops=2
    )


def test_submit_draft(agent, temp_blog_root) -> None:
    """BlogPublicationAgent writes draft to pending and returns submission."""
    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Test Post\n\nThis is a draft.",
            title="Test Post",
            tags=["test"],
        )
    )

    assert result.submission_id
    assert result.slug == "test-post"
    assert result.state == "awaiting_approval"
    assert result.file_path.exists()
    assert result.file_path.read_text() == "# Test Post\n\nThis is a draft."
    assert (temp_blog_root / "pending" / f"{result.submission_id}_meta.json").exists()


def test_approve(agent, temp_blog_root) -> None:
    """BlogPublicationAgent approve creates folder and platform versions."""
    result = agent.submit_draft(
        SubmitDraftInput(draft="# Approved Post\n\nContent here.", title="Approved Post")
    )

    approval = agent.approve(result.submission_id)

    assert approval.submission_id == result.submission_id
    assert approval.folder_path == temp_blog_root / "approved-post"
    assert approval.draft_path.exists()
    assert approval.medium_path.exists()
    assert approval.devto_path.exists()
    assert approval.substack_path.exists()
    assert "title: Approved Post" in approval.devto_path.read_text()
    assert approval.draft_path.read_text() == "# Approved Post\n\nContent here."


def test_reject_and_revision_loop(agent, temp_blog_root) -> None:
    """BlogPublicationAgent reject collects feedback; run_revision_loop revises draft."""
    from blog_copy_editor_agent import BlogCopyEditorAgent
    from blog_writer_agent import BlogWriterAgent

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
            audience="developers",
        )
    )

    rejection = agent.reject(
        result.submission_id, "The intro is too short.", force_ready_to_revise=True
    )
    assert rejection.ready_to_revise

    draft_agent = BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="Use clear sentence flow and plain language.",
        brand_spec_content="Brand voice: practical and trustworthy.",
    )
    copy_editor_agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())

    revision = agent.run_revision_loop(
        result.submission_id,
        draft_agent=draft_agent,
        copy_editor_agent=copy_editor_agent,
        audience="developers",
    )

    assert revision.submission_id == result.submission_id
    assert revision.iterations_completed == 2
    assert revision.revised_draft

    draft_path = temp_blog_root / "pending" / f"{result.submission_id}.md"
    assert draft_path.exists()
    assert draft_path.read_text() == revision.revised_draft
