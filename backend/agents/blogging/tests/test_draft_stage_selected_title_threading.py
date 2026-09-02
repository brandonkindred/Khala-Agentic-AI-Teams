"""Tests that ``run_draft_stage`` threads ``ctx.selected_title`` into the writer.

The draft stage builds every ``WriterInput``/``ReviseWriterInput`` (and every
``revise_from_user_feedback`` call) itself, so the author-chosen title only reaches
the writer's prompt if the stage reads it off the context. These tests pin that
wire — and pin that the field's ``None`` default is a no-op.
"""

from __future__ import annotations

from .conftest import make_stub_editor_class

SELECTED_TITLE = "The Author's Chosen Title"


def _capturing_stub_writer_class(captured_inputs: list) -> type:
    """A BlogWriterAgent stand-in that records each writer input it is handed.

    Preconditions:
        - ``captured_inputs`` is a list the caller owns and reads after the run.
    Postconditions:
        - Returns a class (not an instance) suitable for monkeypatching a module's
          ``BlogWriterAgent`` reference. Every ``run``/``revise`` call appends a
          ``("run" | "revise", input)`` pair to ``captured_inputs``.
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

    return _CapturingStubWriter


def _stub_editor_class_requesting_one_revision() -> type:
    """A BlogCopyEditorAgent stub that rejects the first draft, approves the second,
    so the copy-edit loop's ``ReviseWriterInput`` is built exactly once."""
    from agents.blogging.blog_copy_editor_agent.models import CopyEditorOutput, FeedbackItem

    class _StubEditor:
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

    return _StubEditor


def _run_stage(monkeypatch, *, selected_title, editor_class) -> list:
    """Drive ``run_draft_stage`` with a job-store-free context and return the
    ``(kind, input)`` pairs the stub writer captured."""
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.agent_implementations.pipeline.context import PipelineContext
    from agents.blogging.agent_implementations.pipeline.draft_stage import run_draft_stage
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_profile import resolve_length_policy

    from ._content_plan_test_utils import make_minimal_planning_phase_result

    captured: list = []
    monkeypatch.setattr(v2, "load_style_file", lambda *a, **kw: "guidelines text")
    monkeypatch.setattr(v2, "BlogWriterAgent", _capturing_stub_writer_class(captured))
    monkeypatch.setattr(v2, "BlogCopyEditorAgent", editor_class)

    ppr = make_minimal_planning_phase_result()
    ctx = PipelineContext(
        brief=ResearchBriefInput(brief="Topic about AI", audience="devs"),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(),
        series_context=None,
        # No job store: the HITL steps are skipped and the draft goes straight to
        # the automated copy-edit loop.
        job_id=None,
        job_updater=None,
        draft_editor_iterations=2,
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
    captured = _run_stage(
        monkeypatch,
        selected_title=SELECTED_TITLE,
        editor_class=_stub_editor_class_requesting_one_revision(),
    )

    kinds = [kind for kind, _ in captured]
    assert "run" in kinds and "revise" in kinds
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
