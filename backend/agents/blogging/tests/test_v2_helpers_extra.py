"""More v2 helper coverage: title-selection variants and small gaps."""

from __future__ import annotations

import uuid

import pytest


@pytest.fixture
def patched_client(monkeypatch, fake_job_client):
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    try:
        from blogging.shared import blog_job_store as bjs_alt

        monkeypatch.setattr(bjs_alt, "_client", lambda *a, **kw: fake_job_client)
    except ImportError:
        pass
    return fake_job_client


def _plan(target_reader: str | None = None):
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    plan_kwargs = dict(
        overarching_topic="My Topic",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="A", coverage_description="ax", order=0),
            ContentPlanSection(title="B", coverage_description="bx", order=1),
        ],
        title_candidates=[
            TitleCandidate(title="First", probability_of_success=0.6),
        ],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )
    if target_reader is not None:
        plan_kwargs["target_reader"] = target_reader
    return ContentPlan(**plan_kwargs)


def test_run_title_selection_replaces_disliked_with_llm_replacement(
    monkeypatch, patched_client
) -> None:
    from agent_implementations.blog_writing_process_v2 import _run_title_selection
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    bjs.update_blog_job(
        job_id,
        waiting_for_title_selection=True,
        pending_title_feedback=[{"title": "First", "rating": "dislike"}],
    )

    state = {"call": 0}

    class _LLM:
        def complete_json(self, prompt, **_kw):
            state["call"] += 1
            return {
                "titles": [{"title": "Better Title", "probability_of_success": 0.9}]
            }

    plan_with_reader = _plan(target_reader="senior eng")

    def updater(**kw):
        if state["call"] >= 1:
            bjs.update_blog_job(
                job_id,
                waiting_for_title_selection=False,
                selected_title="Better Title",
            )

    out = _run_title_selection(
        plan=plan_with_reader,
        llm_client=_LLM(),
        job_id=job_id,
        job_updater=updater,
        _update=lambda phase, **kw: updater(**kw),
    )
    assert out == "Better Title"
    assert state["call"] >= 1


def test_run_title_selection_llm_failure_falls_back_to_removal(
    monkeypatch, patched_client
) -> None:
    """If LLM raises while generating a replacement, the disliked title is REMOVED.
    Then the user selects another title (= loves it) and we return it."""
    import agent_implementations.blog_writing_process_v2 as v2
    from agent_implementations.blog_writing_process_v2 import _run_title_selection
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    bjs.update_blog_job(
        job_id,
        waiting_for_title_selection=True,
        pending_title_feedback=[{"title": "First", "rating": "dislike"}],
    )

    class _AngryLLM:
        def complete_json(self, prompt, **_kw):
            raise RuntimeError("transport down")

    sleep_count = {"n": 0}

    def fake_sleep(*_a, **_kw):
        sleep_count["n"] += 1
        bjs.update_blog_job(
            job_id,
            waiting_for_title_selection=False,
            selected_title="Fallback Title",
        )

    monkeypatch.setattr(v2.time, "sleep", fake_sleep)

    out = _run_title_selection(
        plan=_plan(),
        llm_client=_AngryLLM(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        _update=lambda phase, **kw: None,
    )
    assert out == "Fallback Title"


def test_run_title_selection_propagates_cancelled_error(monkeypatch, patched_client) -> None:
    """CancelledError inside the loop propagates out — does not become None."""
    from agent_implementations.blog_writing_process_v2 import _run_title_selection
    from shared import blog_job_store as bjs
    from temporalio.exceptions import CancelledError

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    bjs.update_blog_job(job_id, waiting_for_title_selection=True)

    import agent_implementations.blog_writing_process_v2 as v2

    def angry_sleep(*_a, **_kw):
        raise CancelledError("cancelled")

    monkeypatch.setattr(v2.time, "sleep", angry_sleep)

    with pytest.raises(CancelledError):
        _run_title_selection(
            plan=_plan(),
            llm_client=object(),
            job_id=job_id,
            job_updater=lambda **kw: None,
            _update=lambda phase, **kw: None,
        )


def test_run_title_selection_swallows_generic_error(monkeypatch, patched_client) -> None:
    """Non-Cancelled exceptions inside the function are caught and return None."""
    from agent_implementations.blog_writing_process_v2 import _run_title_selection
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    bjs.update_blog_job(job_id, waiting_for_title_selection=True)

    import agent_implementations.blog_writing_process_v2 as v2

    def angry_sleep(*_a, **_kw):
        raise RuntimeError("transport failed")

    monkeypatch.setattr(v2.time, "sleep", angry_sleep)

    out = _run_title_selection(
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        _update=lambda phase, **kw: None,
    )
    assert out is None
