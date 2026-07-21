"""Tests for blog_writing_process_v2._fill_story_placeholders.

Uses the shared ContentPlan factory from ``_content_plan_test_utils``.
"""

from __future__ import annotations

import uuid

import pytest
from agents.blogging.blog_writer_agent.models import WriterOutput
from agents.blogging.ghost_writer_agent.models import StoryElicitationResult


def _plan():
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate

    from ._content_plan_test_utils import make_content_plan

    return make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="T", probability_of_success=0.5)],
    )


def _make_stub_ghost(
    *,
    narrative: str | None = None,
    skipped: bool = False,
    rounds_used: int = 1,
    raises: bool = False,
):
    """Build a GhostWriterElicitationAgent stand-in returning a canned StoryElicitationResult.

    Pass ``raises=True`` for the "must not be called" case (the cancel-check-breaks-first tests).
    """

    class _StubGhost:
        def __init__(self, *a, **kw):
            pass

        def conduct_interview(self, gap, **kw):
            if raises:
                raise AssertionError("should not be called")
            return StoryElicitationResult(
                gap=gap, narrative=narrative, skipped=skipped, rounds_used=rounds_used
            )

    return _StubGhost


def _make_stub_draft_agent(draft_text: str):
    """Build a draft_agent stand-in whose .run(...) always returns ``draft_text``."""

    class _StubAgent:
        def run(self, *a, **kw):
            return WriterOutput(draft=draft_text)

    return _StubAgent


def test_fill_story_placeholders_no_placeholders_returns_input(monkeypatch) -> None:
    """When no [Author: ...] placeholders exist, return original draft and stories."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    out_draft, out_stories = _fill_story_placeholders(
        draft_text="# Draft\nBody with no placeholders.",
        plan=_plan(),
        llm_client=object(),
        job_id="j1",
        job_updater=lambda **kw: None,
        elicited_stories_text="existing stories",
        draft_agent=object(),
        draft_input_kwargs={},
        work_dir=None,
        iteration=1,
    )
    assert out_draft.draft == "# Draft\nBody with no placeholders."
    assert out_stories == "existing stories"


def test_fill_story_placeholders_user_skips_all(monkeypatch, tmp_path) -> None:
    """User skips all placeholders → re-draft path with skip instruction."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    # Stub GhostWriterElicitationAgent.conduct_interview to return skipped
    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(gw, "GhostWriterElicitationAgent", _make_stub_ghost(skipped=True))

    # Stub draft_agent.run to return the redraft, capturing the WriterInput it
    # was called with so we can confirm the skip instruction was built.
    captured: dict = {}

    class _StubAgent:
        def run(self, draft_input, **kw):
            captured["draft_input"] = draft_input
            return WriterOutput(draft="# Redraft without stories\nBody.")

    out_draft, out_stories = v2._fill_story_placeholders(
        draft_text="# Draft\n[Author: add a real story]\nBody.",
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_StubAgent(),
        draft_input_kwargs={"content_plan": _plan()},
        work_dir=tmp_path,
        iteration=1,
    )
    # The redraft (success) branch must have run — not the exception-fallback
    # branch that keeps the original draft — so the stubbed output is returned
    # verbatim.
    assert out_draft.draft == "# Redraft without stories\nBody."
    # No narrative was collected, so elicited_stories_text is left untouched.
    assert out_stories is None
    # The draft agent must have actually been invoked with the skip
    # instruction naming the skipped topic, proving the skip path was
    # exercised rather than silently no-op'd.
    elicited = captured["draft_input"].elicited_stories
    assert elicited is not None
    assert "NO PERSONAL EXPERIENCE" in elicited
    # _PLACEHOLDER_RE strips a leading "add " from the placeholder body, so
    # the topic recorded in the skip instruction is "a real story".
    assert "a real story" in elicited


def test_fill_story_placeholders_user_provides_narrative(monkeypatch, tmp_path) -> None:
    """User provides a story → narrative collected and re-drafted."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(
        gw,
        "GhostWriterElicitationAgent",
        _make_stub_ghost(narrative="I once debugged a production outage.", rounds_used=2),
    )

    out_draft, out_stories = v2._fill_story_placeholders(
        draft_text="# Draft\n[Author: a debug story]\nBody.",
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_make_stub_draft_agent("# Redraft with stories\nNarrative incorporated.")(),
        draft_input_kwargs={"content_plan": _plan()},
        work_dir=tmp_path,
        iteration=1,
    )
    assert "Redraft" in out_draft.draft
    assert "debugged" in out_stories


def test_fill_story_placeholders_redraft_fails_keeps_original(monkeypatch, tmp_path) -> None:
    """When re-draft raises, keep original draft."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(gw, "GhostWriterElicitationAgent", _make_stub_ghost(narrative="A story."))

    class _Boom:
        def run(self, *a, **kw):
            raise RuntimeError("redraft failed")

    out_draft, out_stories = v2._fill_story_placeholders(
        draft_text="# Draft\n[Author: a story]\nBody.",
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_Boom(),
        draft_input_kwargs={"content_plan": _plan()},
        work_dir=tmp_path,
        iteration=1,
    )
    assert "[Author:" in out_draft.draft  # original kept


def test_fill_story_placeholders_story_bank_save_cancellation_propagates(
    monkeypatch, tmp_path
) -> None:
    """A Temporal cancellation raised from the story-bank save must propagate,
    not be swallowed by the non-fatal save guard."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs
    from agents.blogging.shared import story_bank
    from temporalio.exceptions import CancelledError

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(
        gw, "GhostWriterElicitationAgent", _make_stub_ghost(narrative="A story worth saving.")
    )

    def _cancelling_save(**kw):
        raise CancelledError("cancelled")

    monkeypatch.setattr(story_bank, "save_story", _cancelling_save)

    with pytest.raises(CancelledError):
        v2._fill_story_placeholders(
            draft_text="# Draft\n[Author: a story]\nBody.",
            plan=_plan(),
            llm_client=object(),
            job_id=job_id,
            job_updater=lambda **kw: None,
            elicited_stories_text=None,
            draft_agent=_make_stub_draft_agent("# Should not get here")(),
            draft_input_kwargs={"content_plan": _plan()},
            work_dir=tmp_path,
            iteration=1,
        )


def test_fill_story_placeholders_cancelled_break(monkeypatch, tmp_path) -> None:
    """If job goes to cancelled mid-loop, break out."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(
        job_id,
        "brief",
    )
    bjs.update_blog_job(job_id, status="cancelled")

    # Even though the ghost writer would be invoked, the cancel check breaks first
    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(gw, "GhostWriterElicitationAgent", _make_stub_ghost(raises=True))

    out_draft, _ = v2._fill_story_placeholders(
        draft_text="# Draft\n[Author: a story]\nBody.",
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_make_stub_draft_agent("# Should not get here")(),
        draft_input_kwargs={"content_plan": _plan()},
        work_dir=tmp_path,
        iteration=1,
    )
    # Original kept (no narratives, no skipped → returns WriterOutput with original draft)
    assert "[Author:" in out_draft.draft
