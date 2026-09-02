"""Tests that ``run_draft_stage`` threads ``ctx.selected_title`` into the writer.

The draft stage builds every ``WriterInput``/``ReviseWriterInput`` (and every
``revise_from_user_feedback`` call) itself, so the author-chosen title only reaches
the writer's prompt if the stage reads it off the context. These tests pin all six
construction sites — the two that run without a job store, and the four that need
the HITL flow — and pin that the field's ``None`` default is a no-op.
"""

from __future__ import annotations

from types import SimpleNamespace

from .conftest import make_stub_editor_class

SELECTED_TITLE = "The Author's Chosen Title"

# draft_editor_iterations needed to reach the copy-edit escalation branch:
# the stage's loop sets copy_edit_num = iteration - 1 and escalates when
# copy_edit_num hits COPY_EDIT_ESCALATION_THRESHOLD (10), i.e. iteration 11.
_ESCALATION_ITERATIONS = 11


def _capturing_stub_writer_class(captured_inputs: list, *, uncertainty_questions: list) -> type:
    """A BlogWriterAgent stand-in that records every writer input it is handed.

    Preconditions:
        - ``captured_inputs`` is a list the caller owns and reads after the run.
        - ``uncertainty_questions`` is the list ``identify_uncertainty_questions``
          should return (empty to skip the uncertainty-answer revision path).
    Postconditions:
        - Returns a class (not an instance) suitable for monkeypatching a module's
          ``BlogWriterAgent`` reference. Every ``run``/``revise``/
          ``revise_from_user_feedback`` call appends a ``(kind, input)`` pair to
          ``captured_inputs``, where ``input`` always exposes ``selected_title``.
    """
    from agents.blogging.blog_writer_agent.models import WriterOutput

    class _CapturingStubWriter:
        def __init__(self, *a, **kw):
            pass

        def run(self, draft_input, *a, **kw):
            captured_inputs.append(("run", draft_input))
            return WriterOutput(draft="# Draft\n\nBody.")

        def revise(self, revise_input, *a, **kw):
            captured_inputs.append(("revise", revise_input))
            return WriterOutput(draft="# Revised\n\nBody.")

        def revise_from_user_feedback(self, *a, selected_title=None, **kw):
            # Recorded as a namespace so callers can assert on ``.selected_title``
            # uniformly across all three call shapes.
            captured_inputs.append(
                ("revise_from_user_feedback", SimpleNamespace(selected_title=selected_title))
            )
            return WriterOutput(draft="# Revised\n\nBody.")

        def identify_uncertainty_questions(self, *a, **kw):
            return list(uncertainty_questions)

        def analyze_user_feedback_for_guideline_updates(self, *a, **kw):
            return []

        def generate_escalation_summary(self, *a, **kw):
            return "escalation summary"

    return _CapturingStubWriter


def _never_approving_editor_class() -> type:
    """A BlogCopyEditorAgent stub that never approves and never repeats itself.

    ``FeedbackTracker`` keys an issue by ``(category, severity, location)`` — not by
    its text — and calls the loop stalled once consecutive rounds overlap by >0.80
    Jaccard. Varying ``location`` each round therefore keeps every signature distinct,
    so the loop runs far enough to reach the escalation branch instead of breaking
    early on the stall check.
    """
    from agents.blogging.blog_copy_editor_agent.models import CopyEditorOutput, FeedbackItem

    class _StubEditor:
        def __init__(self, *a, **kw):
            self._calls = 0

        def run(self, *a, **kw):
            self._calls += 1
            return CopyEditorOutput(
                approved=False,
                summary=f"revise round {self._calls}",
                feedback_items=[
                    FeedbackItem(
                        category="style",
                        severity="must_fix",
                        location=f"paragraph {self._calls}",
                        issue=f"Distinct issue {self._calls}.",
                        suggestion=f"Fix issue {self._calls}.",
                    )
                ],
            )

    return _StubEditor


def _install_fake_job_store(monkeypatch, *, draft_feedback_script: list, submitted_answers: list):
    """Monkeypatch the job-store surface so the HITL paths run without a real store.

    ``_wait_for_hitl`` is replaced wholesale (returning False = "user responded, not
    cancelled") so no test blocks on polling. ``draft_feedback_script`` is consumed
    one entry per ``get_user_draft_feedback`` call, falling back to an approval once
    exhausted so no review loop spins forever.
    """
    from agents.blogging.agent_implementations.pipeline import draft_stage as ds
    from agents.blogging.shared import blog_job_store

    monkeypatch.setattr(ds, "_wait_for_hitl", lambda *_a, **_kw: False)
    monkeypatch.setattr(ds, "add_blog_pending_questions", lambda *_a, **_kw: None)
    monkeypatch.setattr(ds, "record_guideline_updates", lambda *_a, **_kw: None)

    # These four are imported inside run_draft_stage, so patch them on the module.
    monkeypatch.setattr(blog_job_store, "request_draft_feedback", lambda *_a, **_kw: None)
    monkeypatch.setattr(blog_job_store, "is_waiting_for_draft_feedback", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        blog_job_store, "get_blog_job", lambda *_a, **_kw: {"submitted_answers": submitted_answers}
    )
    feedback = iter(draft_feedback_script)
    monkeypatch.setattr(
        blog_job_store,
        "get_user_draft_feedback",
        lambda *_a, **_kw: next(feedback, {"approved": True}),
    )


def _spy_fill_story_placeholders(captured_kwargs: list):
    """Stand in for ``_fill_story_placeholders``, recording its ``draft_input_kwargs``.

    The real helper drives a ghost-writer interview; the call site under test is the
    dict the draft stage builds, and this records it without that machinery. It is
    also what pins the key's presence for the ``WriterInput`` the real helper
    reconstructs from those kwargs.
    """
    from agents.blogging.blog_writer_agent.models import WriterOutput

    def _fill(*, draft_text, draft_input_kwargs, elicited_stories_text, **_kw):
        captured_kwargs.append(draft_input_kwargs)
        return WriterOutput(draft=draft_text), elicited_stories_text

    return _fill


def _run_stage(
    monkeypatch,
    *,
    selected_title,
    editor_class,
    job_store: bool = False,
    draft_editor_iterations: int = 2,
) -> list:
    """Drive ``run_draft_stage`` and return the ``(kind, input)`` pairs it produced."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.agent_implementations.pipeline.context import PipelineContext
    from agents.blogging.agent_implementations.pipeline.draft_stage import run_draft_stage
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import resolve_length_policy

    from ._content_plan_test_utils import make_minimal_planning_phase_result

    captured: list = []
    questions = (
        [SimpleNamespace(question_id="q1", question="Which framing?", context="ctx")]
        if job_store
        else []
    )
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "guidelines text")
    monkeypatch.setattr(
        v2,
        "BlogWriterAgent",
        _capturing_stub_writer_class(captured, uncertainty_questions=questions),
    )
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", editor_class)

    ppr = make_minimal_planning_phase_result()
    ctx = PipelineContext(
        brief=ResearchBriefInput(brief="Topic about AI", audience="devs"),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(),
        series_context=None,
        # Without a job store the HITL steps are skipped and the draft goes
        # straight to the automated copy-edit loop.
        job_id="job-1" if job_store else None,
        job_updater=(lambda **_kw: None) if job_store else None,
        draft_editor_iterations=draft_editor_iterations,
        max_rewrite_iterations=1,
        run_gates=False,
        planning_phase_result=ppr,
        plan=ppr.content_plan,
        selected_title=selected_title,
    )

    assert run_draft_stage(ctx) is None
    return captured


def test_draft_stage_threads_selected_title_into_writer_inputs(monkeypatch) -> None:
    """A populated ``ctx.selected_title`` reaches both the initial-draft
    ``WriterInput`` and the copy-edit-loop ``ReviseWriterInput``."""
    from agents.blogging.blog_copy_editor_agent.models import CopyEditorOutput, FeedbackItem

    class _EditorRequestingOneRevision:
        def __init__(self, *a, **kw):
            self._calls = 0

        def run(self, *a, **kw):
            self._calls += 1
            return CopyEditorOutput(
                approved=self._calls > 1,
                summary="revise" if self._calls == 1 else "ok",
                feedback_items=[]
                if self._calls > 1
                else [
                    FeedbackItem(
                        category="style",
                        severity="must_fix",
                        issue="Needs work.",
                        suggestion="Fix it.",
                    )
                ],
            )

    captured = _run_stage(
        monkeypatch, selected_title=SELECTED_TITLE, editor_class=_EditorRequestingOneRevision
    )

    kinds = [kind for kind, _ in captured]
    assert "run" in kinds and "revise" in kinds
    for kind, writer_input in captured:
        assert writer_input.selected_title == SELECTED_TITLE, (
            f"{kind} call did not receive selected_title"
        )


def test_draft_stage_threads_selected_title_through_hitl_paths(monkeypatch) -> None:
    """With a job store, ``selected_title`` reaches the story-placeholder refill
    kwargs, the uncertainty-answer revision, and the author draft-review revision."""
    fill_kwargs: list = []
    from agents.blogging.agent_implementations.pipeline import draft_stage as ds

    monkeypatch.setattr(ds, "_fill_story_placeholders", _spy_fill_story_placeholders(fill_kwargs))
    _install_fake_job_store(
        monkeypatch,
        # First review round asks for changes (driving the author-feedback
        # revision), the second approves so the loop terminates.
        draft_feedback_script=[
            {"approved": False, "feedback": "Tighten the intro."},
            {"approved": True},
        ],
        submitted_answers=[{"question_id": "q1", "selected_answer": "The second framing."}],
    )

    captured = _run_stage(
        monkeypatch,
        selected_title=SELECTED_TITLE,
        editor_class=make_stub_editor_class(),
        job_store=True,
    )

    # The _fill_story_placeholders call site: the dict must carry the key, since the
    # helper splats it into the WriterInput it rebuilds.
    assert fill_kwargs, "story-placeholder refill was never called"
    for kwargs in fill_kwargs:
        assert kwargs["selected_title"] == SELECTED_TITLE

    # Both revise_from_user_feedback sites: uncertainty answers, then author feedback.
    revisions = [inp for kind, inp in captured if kind == "revise_from_user_feedback"]
    assert len(revisions) >= 2, f"expected both HITL revisions, got {len(revisions)}"
    for kind, writer_input in captured:
        assert writer_input.selected_title == SELECTED_TITLE, (
            f"{kind} call did not receive selected_title"
        )


def test_draft_stage_threads_selected_title_into_escalation_revision(monkeypatch) -> None:
    """The copy-edit escalation revision also receives ``selected_title``.

    Reaching it needs a job store plus an editor that never approves, so the loop
    runs to the escalation threshold.
    """
    fill_kwargs: list = []
    from agents.blogging.agent_implementations.pipeline import draft_stage as ds

    monkeypatch.setattr(ds, "_fill_story_placeholders", _spy_fill_story_placeholders(fill_kwargs))
    _install_fake_job_store(
        monkeypatch,
        # Approve at draft review so the run reaches the copy-edit loop, then
        # return feedback at the escalation prompt to drive its revision.
        draft_feedback_script=[
            {"approved": True},
            {"approved": False, "feedback": "Still needs a rewrite."},
        ],
        submitted_answers=[],
    )

    captured = _run_stage(
        monkeypatch,
        selected_title=SELECTED_TITLE,
        editor_class=_never_approving_editor_class(),
        job_store=True,
        draft_editor_iterations=_ESCALATION_ITERATIONS,
    )

    revisions = [inp for kind, inp in captured if kind == "revise_from_user_feedback"]
    assert revisions, "escalation revision never fired"
    for kind, writer_input in captured:
        assert writer_input.selected_title == SELECTED_TITLE, (
            f"{kind} call did not receive selected_title"
        )


def test_draft_stage_selected_title_default_none_is_noop(monkeypatch) -> None:
    """The field's ``None`` default leaves the writer free to choose a title."""
    captured = _run_stage(monkeypatch, selected_title=None, editor_class=make_stub_editor_class())

    assert captured
    for _kind, writer_input in captured:
        assert writer_input.selected_title is None
