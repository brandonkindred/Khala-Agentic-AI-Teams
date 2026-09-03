"""Tests for blog_writing_process_v2._fill_story_placeholders.

Uses the shared ContentPlan factory from ``_content_plan_test_utils``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

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
    """Build a draft_agent stand-in whose .revise_from_user_feedback(...) always
    returns ``draft_text``."""

    class _StubAgent:
        def revise_from_user_feedback(self, *a, **kw):
            return WriterOutput(draft=draft_text)

    return _StubAgent


def _full_draft_input_kwargs(**overrides):
    """The full ``draft_input_kwargs`` shape the real call site builds
    (``draft_stage.py``), so the revision call's direct-subscript reads succeed."""
    base = dict(
        content_plan=_plan(),
        audience="developers",
        tone_or_purpose="informative",
        target_word_count=1000,
        length_guidance="Aim for 1000 words.",
        selected_title=None,
        allowed_claims=None,
    )
    base.update(overrides)
    return base


def _valid_fill_kwargs(**overrides):
    """Minimal valid kwargs for `_fill_story_placeholders` guard tests.

    Preconditions:
        - Overrides only replace keys that `_fill_story_placeholders` accepts.
    Postconditions:
        - Returns a complete kwargs dict that satisfies the happy-path contract
          unless an override intentionally violates it.
    """
    base = dict(
        draft_text="# Draft\nBody with no placeholders.",
        plan=_plan(),
        llm_client=object(),
        job_id="j1",
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_make_stub_draft_agent("unused")(),
        draft_input_kwargs={},
        work_dir=None,
        iteration=1,
    )
    base.update(overrides)
    return base


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
        draft_agent=_make_stub_draft_agent("unused")(),
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

    # Stub draft_agent.revise_from_user_feedback to return the revision,
    # capturing the kwargs it was called with so we can confirm the skip
    # instruction was built into user_feedback.
    captured: dict = {}

    class _StubAgent:
        def revise_from_user_feedback(self, **kw):
            captured["kwargs"] = kw
            return WriterOutput(draft="# Redraft without stories\nBody.")

    out_draft, out_stories = v2._fill_story_placeholders(
        draft_text="# Draft\n[Author: add a real story]\nBody.",
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_StubAgent(),
        draft_input_kwargs=_full_draft_input_kwargs(),
        work_dir=tmp_path,
        iteration=1,
    )
    # The revision (success) branch must have run — not the exception-fallback
    # branch that keeps the original draft — so the stubbed output is returned
    # verbatim.
    assert out_draft.draft == "# Redraft without stories\nBody."
    # No narrative was collected, so elicited_stories_text is left untouched.
    assert out_stories is None
    # The draft agent must have actually been invoked with the skip
    # instruction naming the skipped topic, proving the skip path was
    # exercised rather than silently no-op'd.
    user_feedback = captured["kwargs"]["user_feedback"]
    assert "NO PERSONAL EXPERIENCE" in user_feedback
    # _PLACEHOLDER_RE strips a leading "add " from the placeholder body, so
    # the topic recorded in the skip instruction is "a real story".
    assert "a real story" in user_feedback
    # No narrative was collected on this skip-only path, so the running
    # elicited_stories total passed through to the revision stays None.
    assert captured["kwargs"]["elicited_stories"] is None
    # The remaining draft_input_kwargs-sourced fields must be read from
    # draft_input_kwargs, not hard-coded or dropped — checked against the
    # distinctive values _full_draft_input_kwargs() pins.
    assert captured["kwargs"]["audience"] == "developers"
    assert captured["kwargs"]["tone_or_purpose"] == "informative"
    assert captured["kwargs"]["target_word_count"] == 1000
    assert captured["kwargs"]["length_guidance"] == "Aim for 1000 words."
    assert captured["kwargs"]["selected_title"] is None
    assert captured["kwargs"]["allowed_claims"] is None


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

    # Capture the kwargs revise_from_user_feedback was called with so we can
    # confirm the newly collected narrative is forwarded via elicited_stories
    # (not just merged into the return value).
    captured: dict = {}

    class _StubAgent:
        def revise_from_user_feedback(self, **kw):
            captured["kwargs"] = kw
            return WriterOutput(draft="# Redraft with stories\nNarrative incorporated.")

    out_draft, out_stories = v2._fill_story_placeholders(
        draft_text="# Draft\n[Author: a debug story]\nBody.",
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_StubAgent(),
        draft_input_kwargs=_full_draft_input_kwargs(),
        work_dir=tmp_path,
        iteration=1,
    )
    assert "Redraft" in out_draft.draft
    assert "debugged" in out_stories
    assert "debugged" in captured["kwargs"]["elicited_stories"]
    # content_plan_text must be built via content_plan_to_outline_markdown(plan),
    # matching the two existing revise_from_user_feedback call sites in draft_stage.py.
    from agents.blogging.shared.content_plan import content_plan_to_outline_markdown

    assert captured["kwargs"]["content_plan_text"] == content_plan_to_outline_markdown(_plan())


def test_fill_story_placeholders_preserves_prefill_draft_artifact(monkeypatch, tmp_path) -> None:
    """The pre-fill draft_v1.md (written by the caller before this helper runs)
    must survive on disk, distinct from the post-fill draft_v1_stories.md this
    helper writes -- the helper must not clobber the draft it revises from."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(
        gw, "GhostWriterElicitationAgent", _make_stub_ghost(narrative="A debug story.")
    )

    # Simulate what draft_stage.py already did before calling this helper:
    # draft_v1.md holds the reviewed pre-fill draft.
    prefill_content = "# Pre-fill draft\n[Author: a debug story]\nBody."
    (tmp_path / "draft_v1.md").write_text(prefill_content, encoding="utf-8")

    class _StubAgent:
        def revise_from_user_feedback(self, *, draft_output_path=None, **kw):
            content = "# Post-fill draft\nNarrative incorporated."
            if draft_output_path is not None:
                Path(draft_output_path).write_text(content, encoding="utf-8")
            return WriterOutput(draft=content)

    out_draft, _ = v2._fill_story_placeholders(
        draft_text=prefill_content,
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_StubAgent(),
        draft_input_kwargs=_full_draft_input_kwargs(),
        work_dir=tmp_path,
        iteration=1,
    )

    assert out_draft.draft == "# Post-fill draft\nNarrative incorporated."
    # The pre-fill draft is untouched...
    assert (tmp_path / "draft_v1.md").read_text(encoding="utf-8") == prefill_content
    # ...and the post-fill revision landed at a distinct path, not draft_v1.md.
    assert (tmp_path / "draft_v1_stories.md").read_text(
        encoding="utf-8"
    ) == "# Post-fill draft\nNarrative incorporated."


def test_fill_story_placeholders_redraft_fails_keeps_original(monkeypatch, tmp_path) -> None:
    """When re-draft raises, keep original draft."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(gw, "GhostWriterElicitationAgent", _make_stub_ghost(narrative="A story."))

    class _Boom:
        def revise_from_user_feedback(self, *a, **kw):
            raise RuntimeError("redraft failed")

    out_draft, out_stories = v2._fill_story_placeholders(
        draft_text="# Draft\n[Author: a story]\nBody.",
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        elicited_stories_text=None,
        draft_agent=_Boom(),
        draft_input_kwargs=_full_draft_input_kwargs(),
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
            draft_input_kwargs=_full_draft_input_kwargs(),
            work_dir=tmp_path,
            iteration=1,
        )


def test_fill_story_placeholders_redraft_cancellation_propagates(monkeypatch, tmp_path) -> None:
    """A Temporal cancellation raised from the post-story revision call must
    propagate, not be swallowed into the "keep original draft" fallback."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs
    from temporalio.exceptions import CancelledError

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(gw, "GhostWriterElicitationAgent", _make_stub_ghost(narrative="A story."))

    class _CancellingAgent:
        def revise_from_user_feedback(self, *a, **kw):
            raise CancelledError("cancelled")

    with pytest.raises(CancelledError):
        v2._fill_story_placeholders(
            draft_text="# Draft\n[Author: a story]\nBody.",
            plan=_plan(),
            llm_client=object(),
            job_id=job_id,
            job_updater=lambda **kw: None,
            elicited_stories_text=None,
            draft_agent=_CancellingAgent(),
            draft_input_kwargs=_full_draft_input_kwargs(),
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
        draft_input_kwargs=_full_draft_input_kwargs(),
        work_dir=tmp_path,
        iteration=1,
    )
    # Original kept (no narratives, no skipped → returns WriterOutput with original draft)
    assert "[Author:" in out_draft.draft


def test_fill_story_placeholders_progress_stays_below_next_phase(monkeypatch, tmp_path) -> None:
    """With more placeholders than the old +idx headroom, per-gap progress must
    stay below the 40 the next phase ("draft_initial") reports, so progress
    never regresses."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    import agents.blogging.ghost_writer_agent as gw

    monkeypatch.setattr(gw, "GhostWriterElicitationAgent", _make_stub_ghost(skipped=True))

    draft_text = "# Draft\n" + "\n".join(f"[Author: story {i}]" for i in range(6)) + "\nBody."

    progress_values: list[int] = []

    def _capture_updater(**kw):
        progress_values.append(kw["progress"])

    v2._fill_story_placeholders(
        draft_text=draft_text,
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=_capture_updater,
        elicited_stories_text=None,
        draft_agent=_make_stub_draft_agent("# Redraft\nBody.")(),
        draft_input_kwargs=_full_draft_input_kwargs(),
        work_dir=tmp_path,
        iteration=1,
    )
    story_elicitation_progress = progress_values[:-1]
    assert all(p < 40 for p in story_elicitation_progress)
    assert progress_values[-1] == 40


def test_fill_story_placeholders_rejects_non_str_draft_text() -> None:
    """draft_text must be a str — fail before placeholder scanning."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    with pytest.raises(TypeError, match="draft_text must be a string"):
        _fill_story_placeholders(**_valid_fill_kwargs(draft_text=123))  # type: ignore[arg-type]


def test_fill_story_placeholders_rejects_elicited_stories_in_kwargs() -> None:
    """draft_input_kwargs must not already contain elicited_stories."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    with pytest.raises(ValueError, match="elicited_stories"):
        _fill_story_placeholders(
            **_valid_fill_kwargs(draft_input_kwargs={"elicited_stories": "pre-set"})
        )


def test_fill_story_placeholders_rejects_missing_draft_input_kwargs_keys() -> None:
    """draft_input_kwargs must carry every key revise_from_user_feedback needs,
    checked once a placeholder is actually found (kwargs go unused otherwise)."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    with pytest.raises(ValueError, match="missing required keys") as excinfo:
        _fill_story_placeholders(
            **_valid_fill_kwargs(
                draft_text="# Draft\n[Author: a story]\nBody.",
                draft_input_kwargs={"content_plan": _plan()},
            )
        )
    # The message must actually name the missing keys, not just carry the
    # generic prefix — otherwise a regression that reports an empty or
    # partial list would still pass this test.
    message = str(excinfo.value)
    for key in (
        "audience",
        "tone_or_purpose",
        "selected_title",
        "allowed_claims",
        "target_word_count",
        "length_guidance",
    ):
        assert key in message


def test_fill_story_placeholders_ignores_incomplete_kwargs_without_placeholders() -> None:
    """draft_input_kwargs is only validated once a placeholder is found — a
    draft with no placeholders must not raise even with incomplete kwargs,
    since draft_input_kwargs goes unused on that path."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    out_draft, out_stories = _fill_story_placeholders(
        **_valid_fill_kwargs(
            draft_text="# Draft\nBody with no placeholders.",
            draft_input_kwargs={"content_plan": _plan()},
        )
    )
    assert out_draft.draft == "# Draft\nBody with no placeholders."
    assert out_stories is None


def test_fill_story_placeholders_rejects_non_content_plan() -> None:
    """plan must be a ContentPlan instance."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    with pytest.raises(TypeError, match="ContentPlan"):
        _fill_story_placeholders(**_valid_fill_kwargs(plan=object()))  # type: ignore[arg-type]


def test_fill_story_placeholders_rejects_none_llm_client() -> None:
    """llm_client must not be None."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    with pytest.raises(TypeError, match="llm_client must not be None"):
        _fill_story_placeholders(**_valid_fill_kwargs(llm_client=None))


def test_fill_story_placeholders_rejects_draft_agent_without_revise_from_user_feedback() -> None:
    """draft_agent must provide a callable revise_from_user_feedback method."""
    from agents.blogging.agent_implementations.blog_writing_process_v2 import (
        _fill_story_placeholders,
    )

    with pytest.raises(TypeError, match="callable revise_from_user_feedback"):
        _fill_story_placeholders(**_valid_fill_kwargs(draft_agent=object()))  # type: ignore[arg-type]
