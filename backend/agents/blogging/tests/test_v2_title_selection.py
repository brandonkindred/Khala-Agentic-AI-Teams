"""Tests for blog_writing_process_v2._run_title_selection.

The helper handles a multi-round rating workflow. We exercise:
* job missing job_id → returns None immediately
* job cancelled mid-wait → returns None
* title selected (user "loved" a title) → returns title
* pending like/dislike feedback → triggers replacement title generation
"""

from __future__ import annotations

import uuid

import pytest


def _plan():
    from shared.content_plan import (
        ContentPlan,
        ContentPlanSection,
        RequirementsAnalysis,
        TitleCandidate,
    )

    return ContentPlan(
        overarching_topic="My Topic",
        narrative_flow="flow",
        sections=[
            ContentPlanSection(title="A", coverage_description="ax", order=0),
            ContentPlanSection(title="B", coverage_description="bx", order=1),
        ],
        title_candidates=[
            TitleCandidate(title="First", probability_of_success=0.6),
            TitleCandidate(title="Second", probability_of_success=0.5),
        ],
        requirements_analysis=RequirementsAnalysis(
            plan_acceptable=True, scope_feasible=True, research_gaps=[]
        ),
    )


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


def test_run_title_selection_returns_none_without_job_id() -> None:
    from agent_implementations.blog_writing_process_v2 import _run_title_selection

    out = _run_title_selection(
        plan=_plan(),
        llm_client=object(),
        job_id=None,
        job_updater=None,
        _update=lambda phase, **kw: None,
    )
    assert out is None


def test_run_title_selection_returns_loved_title(monkeypatch, patched_client) -> None:
    """User submits 'love' rating → selected_title set, function returns it."""
    from agent_implementations.blog_writing_process_v2 import _run_title_selection
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")

    # Pre-set state: waiting_for_title_selection=True, selected_title=None
    bjs.update_blog_job(job_id, waiting_for_title_selection=True)

    state = {"call": 0}

    def updater(**kw):
        state["call"] += 1
        # On first call, user submits a "love" rating via submit_title_ratings
        if state["call"] == 1:
            bjs.submit_title_ratings(
                job_id,
                [{"title": "First", "rating": "love"}],
            )

    out = _run_title_selection(
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=updater,
        _update=lambda phase, **kw: updater(**kw),
    )
    assert out == "First"


def test_run_title_selection_returns_none_on_cancellation(monkeypatch, patched_client) -> None:
    """When the job is cancelled mid-wait, return None."""
    from agent_implementations.blog_writing_process_v2 import _run_title_selection
    from shared import blog_job_store as bjs

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    bjs.update_blog_job(job_id, waiting_for_title_selection=True, status="cancelled")

    out = _run_title_selection(
        plan=_plan(),
        llm_client=object(),
        job_id=job_id,
        job_updater=lambda **kw: None,
        _update=lambda phase, **kw: None,
    )
    assert out is None


def test_run_title_selection_processes_pending_feedback(monkeypatch, patched_client) -> None:
    """User dislikes title → LLM generates replacement → process continues until 'love'."""
    import agent_implementations.blog_writing_process_v2 as v2
    from shared import blog_job_store as bjs

    # Speed up the sleep
    monkeypatch.setattr(v2.time, "sleep", lambda s: None)

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    bjs.update_blog_job(job_id, waiting_for_title_selection=True)

    class _LLM:
        def complete_json(self, prompt, **kw):
            return {"titles": [{"title": "Replacement Title", "probability_of_success": 0.9}]}

    # Submit a "like" rating first (no love → triggers replacement), then submit a "love" rating
    state = {"call": 0}

    def updater(**kw):
        state["call"] += 1
        if state["call"] == 1:
            # First updater call: user dislikes the first title
            bjs.submit_title_ratings(
                job_id,
                [{"title": "First", "rating": "dislike"}],
            )
        elif state["call"] >= 2:
            # After replacement, user loves the new title
            bjs.submit_title_ratings(
                job_id,
                [{"title": "Replacement Title", "rating": "love"}],
            )

    out = v2._run_title_selection(
        plan=_plan(),
        llm_client=_LLM(),
        job_id=job_id,
        job_updater=updater,
        _update=lambda phase, **kw: updater(**kw),
    )
    assert out == "Replacement Title"


def test_run_title_selection_handles_llm_failure(monkeypatch, patched_client) -> None:
    """If LLM fails to generate replacement, just remove the rated title."""
    import agent_implementations.blog_writing_process_v2 as v2
    from shared import blog_job_store as bjs

    monkeypatch.setattr(v2.time, "sleep", lambda s: None)

    job_id = str(uuid.uuid4())[:8]
    bjs.create_blog_job(job_id, "brief")
    bjs.update_blog_job(job_id, waiting_for_title_selection=True)

    class _BoomLLM:
        def complete_json(self, prompt, **kw):
            raise RuntimeError("LLM down")

    state = {"call": 0}

    def updater(**kw):
        state["call"] += 1
        if state["call"] == 1:
            bjs.submit_title_ratings(
                job_id,
                [{"title": "First", "rating": "like"}],
            )
        elif state["call"] >= 2:
            bjs.submit_title_ratings(
                job_id,
                [{"title": "Second", "rating": "love"}],
            )

    out = v2._run_title_selection(
        plan=_plan(),
        llm_client=_BoomLLM(),
        job_id=job_id,
        job_updater=updater,
        _update=lambda phase, **kw: updater(**kw),
    )
    assert out == "Second"
