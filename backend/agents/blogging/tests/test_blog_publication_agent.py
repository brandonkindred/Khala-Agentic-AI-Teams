"""Tests for the blog publication agent."""

import pytest
from agents.blogging.blog_publication_agent import (
    BlogPublicationAgent,
    SubmitDraftInput,
)

from llm_service import DummyLLMClient

from .conftest import make_writer_agent


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
    """Reject collects feedback; empty structured conversion still drives one revise.

    When the LLM cannot convert rejection text into structured items, the loop
    synthesizes a deterministic must_fix from the raw rejection so the draft is
    actually revised instead of clearing the rejection with zero iterations.
    """
    from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent

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

    draft_agent = make_writer_agent(
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
    assert revision.iterations_completed >= 1
    assert "revised" in revision.message.lower() or revision.iterations_completed >= 1

    draft_path = temp_blog_root / "pending" / f"{result.submission_id}.md"
    assert draft_path.exists()
    assert draft_path.read_text() == revision.revised_draft

    # Rejection feedback was consumed after a successful revise.
    meta_path = temp_blog_root / "pending" / f"{result.submission_id}_meta.json"
    import json

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["rejection_feedback"] == []
    assert meta["state"] == "awaiting_approval"


def test_revision_loop_stops_after_editor_approval(agent, temp_blog_root, monkeypatch) -> None:
    """After one revise, an approved copy-editor result ends the loop early."""
    from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorOutput
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent import BlogWriterAgent, WriterOutput

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
            audience="developers",
        )
    )
    agent.reject(result.submission_id, "The intro is too short.", force_ready_to_revise=True)

    calls = {"editor": 0, "revise": 0}

    def _fake_editor_run(self, copy_editor_input, **_kw):
        calls["editor"] += 1
        if calls["editor"] == 1:
            return CopyEditorOutput(
                approved=False,
                summary="needs a longer intro",
                feedback_items=[
                    FeedbackItem(
                        category="structure",
                        severity="must_fix",
                        location="intro",
                        issue="Intro is too short",
                        suggestion="Add context",
                    )
                ],
            )
        return CopyEditorOutput(approved=True, summary="looks good", feedback_items=[])

    def _fake_revise(self, revise_input):
        calls["revise"] += 1
        return WriterOutput(draft=revise_input.draft + "\n\nRevised.")

    monkeypatch.setattr(BlogCopyEditorAgent, "run", _fake_editor_run)
    monkeypatch.setattr(BlogWriterAgent, "revise", _fake_revise)
    # Convert rejection → structured feedback: return empty so iteration 0 uses
    # only the mocked editor's must_fix item (avoids DummyLLM JSON noise).
    monkeypatch.setattr(
        "agents.blogging.blog_publication_agent.agent.run_json_gate",
        lambda *_a, **_kw: {"feedback_items": []},
    )

    draft_agent = make_writer_agent(
        writing_style_guide_content="clear",
        brand_spec_content="brand",
    )
    copy_editor_agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())

    revision = agent.run_revision_loop(
        result.submission_id,
        draft_agent=draft_agent,
        copy_editor_agent=copy_editor_agent,
        audience="developers",
    )

    assert calls["editor"] == 2
    assert calls["revise"] == 1
    assert revision.iterations_completed == 1
    assert "Revised." in revision.revised_draft


def test_reject_json_parse_exhaustion_falls_back_ready_to_revise(
    agent, temp_blog_root, monkeypatch
) -> None:
    """Unparseable follow-up JSON retries then soft-falls back to ready_to_revise."""
    import json

    from agents.blogging.shared import json_retry as jr_mod

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
        )
    )

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json at all"

    monkeypatch.setattr(jr_mod, "Agent", _Agent)

    rejection = agent.reject(result.submission_id, "The intro is too short.")
    assert rejection.ready_to_revise is True
    assert rejection.questions == []
    assert rejection.submission_id == result.submission_id

    meta_path = temp_blog_root / "pending" / f"{result.submission_id}_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "The intro is too short." in meta["rejection_feedback"]


def test_reject_questions_string_coerced_to_list(agent, monkeypatch) -> None:
    """A string ``questions`` value from the LLM is split into a list."""
    from agents.blogging.shared import json_retry as jr_mod

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
        )
    )

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return (
                '{"ready_to_revise": false, '
                '"questions": "Which section feels off?\\nWhat tone do you want?", '
                '"feedback_summary": ""}'
            )

    monkeypatch.setattr(jr_mod, "Agent", _Agent)

    rejection = agent.reject(result.submission_id, "Tone is wrong.")
    assert rejection.ready_to_revise is False
    assert rejection.questions == ["Which section feels off?", "What tone do you want?"]


@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_reject_event_loop_exception_unwraps_transient(agent, monkeypatch, kind) -> None:
    """Wrapped transient LLM errors re-raise the unwrapped cause, not soft-fallback."""
    from agents.blogging.shared import json_retry as jr_mod
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError
    cause = err_cls("transient outage")

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
        )
    )

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise EventLoopException(cause)

    monkeypatch.setattr(jr_mod, "Agent", _Agent)

    with pytest.raises(err_cls) as exc_info:
        agent.reject(result.submission_id, "Tone is wrong.")
    assert exc_info.value is cause


@pytest.mark.parametrize("kind", ["rate_limit", "temporary"])
def test_revision_loop_event_loop_exception_unwraps_transient(agent, monkeypatch, kind) -> None:
    """Convert-step wrapped transient errors re-raise instead of synthesizing feedback."""
    from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent
    from agents.blogging.blog_writer_agent import BlogWriterAgent
    from agents.blogging.shared import json_retry as jr_mod
    from strands.types.exceptions import EventLoopException

    from llm_service import LLMRateLimitError, LLMTemporaryError

    err_cls = LLMRateLimitError if kind == "rate_limit" else LLMTemporaryError
    cause = err_cls("transient outage")

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
            audience="developers",
        )
    )
    agent.reject(result.submission_id, "The intro is too short.", force_ready_to_revise=True)

    class _Agent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            raise EventLoopException(cause)

    monkeypatch.setattr(jr_mod, "Agent", _Agent)

    draft_agent = BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="clear",
        brand_spec_content="brand",
    )
    copy_editor_agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())

    with pytest.raises(err_cls) as exc_info:
        agent.run_revision_loop(
            result.submission_id,
            draft_agent=draft_agent,
            copy_editor_agent=copy_editor_agent,
            audience="developers",
        )
    assert exc_info.value is cause


def test_revision_loop_convert_json_parse_exhaustion_synthesizes_must_fix(
    agent, temp_blog_root, monkeypatch
) -> None:
    """Unparseable convert JSON falls back to empty items; raw rejection drives revise."""
    from agents.blogging.blog_copy_editor_agent import BlogCopyEditorAgent, CopyEditorOutput
    from agents.blogging.blog_copy_editor_agent.models import FeedbackItem
    from agents.blogging.blog_writer_agent import BlogWriterAgent, WriterOutput
    from agents.blogging.shared import json_retry as jr_mod

    result = agent.submit_draft(
        SubmitDraftInput(
            draft="# Rejected Post\n\nNeeds work.",
            title="Rejected Post",
            audience="developers",
        )
    )
    agent.reject(result.submission_id, "The intro is too short.", force_ready_to_revise=True)

    class _BadConvertAgent:
        def __init__(self, *a, **kw):
            pass

        def __call__(self, prompt):
            return "not json at all"

    monkeypatch.setattr(jr_mod, "Agent", _BadConvertAgent)

    calls = {"editor": 0, "revise": 0}

    def _fake_editor_run(self, copy_editor_input, **_kw):
        calls["editor"] += 1
        if calls["editor"] == 1:
            return CopyEditorOutput(
                approved=False,
                summary="needs a longer intro",
                feedback_items=[
                    FeedbackItem(
                        category="structure",
                        severity="must_fix",
                        location="intro",
                        issue="Intro is too short",
                        suggestion="Add context",
                    )
                ],
            )
        return CopyEditorOutput(approved=True, summary="looks good", feedback_items=[])

    def _fake_revise(self, revise_input):
        calls["revise"] += 1
        # Prove the synthesised human must_fix reached revise on iteration 0.
        assert any(
            "intro is too short" in item.issue.lower() for item in revise_input.feedback_items
        )
        return WriterOutput(draft=revise_input.draft + "\n\nRevised.")

    monkeypatch.setattr(BlogCopyEditorAgent, "run", _fake_editor_run)
    monkeypatch.setattr(BlogWriterAgent, "revise", _fake_revise)

    draft_agent = BlogWriterAgent(
        llm_client=DummyLLMClient(),
        writing_style_guide_content="clear",
        brand_spec_content="brand",
    )
    copy_editor_agent = BlogCopyEditorAgent(llm_client=DummyLLMClient())

    revision = agent.run_revision_loop(
        result.submission_id,
        draft_agent=draft_agent,
        copy_editor_agent=copy_editor_agent,
        audience="developers",
    )

    assert calls["revise"] >= 1
    assert revision.iterations_completed >= 1
    assert "Revised." in revision.revised_draft
