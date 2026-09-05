"""Tests for run_planning_stage's title-selection call, wired in after outline approval.

Covers the story that moves the title-selection *invocation* to the end of the
planning stage (``_run_title_selection`` itself is untouched and tested separately
in ``test_v2_title_selection.py`` / ``test_v2_helpers_extra.py``).
"""

from __future__ import annotations

import pytest
from temporalio.exceptions import CancelledError

from ._content_plan_test_utils import make_content_plan, make_planning_phase_result

_UNSET = object()


def _make_ctx(monkeypatch, *, job_id="job-1", job_updater=_UNSET):
    """Build a PipelineContext that sails through story elicitation and a single,
    immediately-approved outline round, landing on the title-selection call.
    """
    import agents.blogging.agent_implementations.blog_writing_process_v2 as v2
    from agents.blogging.agent_implementations.pipeline.context import PipelineContext
    from agents.blogging.blog_research_agent.models import ResearchBriefInput
    from agents.blogging.shared.content_plan import ContentPlanSection, TitleCandidate
    from agents.blogging.shared.content_profile import resolve_length_policy

    plan = make_content_plan(
        overarching_topic="Topic",
        narrative_flow="flow",
        sections=[ContentPlanSection(title="Intro", coverage_description="hook", order=0)],
        title_candidates=[TitleCandidate(title="My Title", probability_of_success=0.7)],
    )
    ppr = make_planning_phase_result(
        plan, planning_iterations_used=1, planning_wall_ms_total=10.0, plan_critic_report=None
    )
    monkeypatch.setattr(v2, "run_planning", lambda *_a, **_kw: ppr)

    # No story-gap elicitation for these tests.
    from agents.blogging import ghost_writer_agent

    monkeypatch.setattr(
        ghost_writer_agent.GhostWriterElicitationAgent,
        "find_story_gaps",
        lambda self, _plan: [],
    )
    from agents.blogging.shared import story_bank

    monkeypatch.setattr(story_bank, "find_relevant_stories", lambda *_a, **_kw: [])

    # Outline approval: the wait returns immediately (not terminal) and the user
    # approves on the first round, so the loop exits without a re-plan.
    from agents.blogging.agent_implementations.pipeline import planning_stage

    monkeypatch.setattr(planning_stage, "_wait_for_hitl", lambda *_a, **_kw: False)

    import agents.blogging.shared.blog_job_store as blog_job_store

    monkeypatch.setattr(blog_job_store, "request_draft_feedback", lambda *_a, **_kw: None)
    monkeypatch.setattr(blog_job_store, "is_waiting_for_draft_feedback", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        blog_job_store, "get_user_draft_feedback", lambda *_a, **_kw: {"approved": True}
    )

    return PipelineContext(
        brief=ResearchBriefInput(brief="A post about testing"),
        work_dir=None,
        llm_client=object(),
        length_policy=resolve_length_policy(),
        series_context=None,
        job_id=job_id,
        job_updater=job_updater if job_updater is not _UNSET else (lambda **_kw: None),
        draft_editor_iterations=1,
        max_rewrite_iterations=1,
        run_gates=False,
    )


def test_selected_title_set_from_title_selection_result(monkeypatch) -> None:
    """A title chosen by _run_title_selection lands on ctx.selected_title."""
    from agents.blogging.agent_implementations.pipeline import planning_stage
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    ctx = _make_ctx(monkeypatch)
    monkeypatch.setattr(planning_stage, "_run_title_selection", lambda *_a, **_kw: "Loved Title")

    result = run_planning_stage(ctx)

    assert result is None
    assert ctx.selected_title == "Loved Title"
    assert ctx.status == "PASS"


def test_selected_title_stays_none_when_title_selection_skipped(monkeypatch) -> None:
    """Without a job store (job_id/job_updater None), title selection is a no-op."""
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    ctx = _make_ctx(monkeypatch, job_id=None, job_updater=None)

    # Story elicitation and outline approval are both gated on job_id/job_updater
    # being non-None in run_planning_stage, so they're skipped entirely here too —
    # only the planning call itself runs.
    result = run_planning_stage(ctx)

    assert result is None
    assert ctx.selected_title is None
    assert ctx.plan is not None


def test_cancelled_error_from_title_selection_propagates(monkeypatch) -> None:
    """CancelledError raised out of _run_title_selection must not be swallowed here."""
    from agents.blogging.agent_implementations.pipeline import planning_stage
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    ctx = _make_ctx(monkeypatch)

    def _boom(*_a, **_kw):
        raise CancelledError("cancel")

    monkeypatch.setattr(planning_stage, "_run_title_selection", _boom)

    with pytest.raises(CancelledError):
        run_planning_stage(ctx)


def test_outline_abort_skips_title_selection_and_keeps_fail_sentinel(monkeypatch) -> None:
    """A terminal outline-approval wait must still short-circuit before title selection."""
    from agents.blogging.agent_implementations.pipeline import planning_stage
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    ctx = _make_ctx(monkeypatch)
    # Terminal: the job was cancelled/failed (or vanished) while awaiting outline approval.
    monkeypatch.setattr(planning_stage, "_wait_for_hitl", lambda *_a, **_kw: True)

    calls: list[object] = []

    def _unexpected(*_a, **_kw):
        calls.append(True)
        return "should not be reached"

    monkeypatch.setattr(planning_stage, "_run_title_selection", _unexpected)

    result = run_planning_stage(ctx)

    assert calls == []
    assert result is not None
    _, draft, status = result
    assert draft is None
    assert status == "FAIL"


def test_title_selection_called_with_stage_bound_values(monkeypatch) -> None:
    """The call forwards this stage's own plan/llm_client/job_id/job_updater/_update."""
    from agents.blogging.agent_implementations.pipeline import planning_stage
    from agents.blogging.agent_implementations.pipeline.planning_stage import run_planning_stage

    llm_client = object()

    def job_updater(**_kw):
        pass

    ctx = _make_ctx(monkeypatch, job_id="job-42", job_updater=job_updater)
    ctx.llm_client = llm_client

    captured: dict = {}

    def _capture(*, plan, llm_client, job_id, job_updater, _update):
        captured.update(
            plan=plan,
            llm_client=llm_client,
            job_id=job_id,
            job_updater=job_updater,
            _update=_update,
        )
        return None

    monkeypatch.setattr(planning_stage, "_run_title_selection", _capture)

    run_planning_stage(ctx)

    assert captured["plan"] is ctx.plan
    assert captured["llm_client"] is llm_client
    assert captured["job_id"] == "job-42"
    assert captured["job_updater"] is job_updater
    assert callable(captured["_update"])
