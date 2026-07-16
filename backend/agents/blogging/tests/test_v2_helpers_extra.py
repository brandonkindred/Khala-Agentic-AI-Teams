"""More v2 helper coverage: title-selection variants and small gaps."""

from __future__ import annotations

import uuid

import pytest
from conftest import make_content_plan


def _plan(target_reader: str | None = None):
    from shared.content_plan import ContentPlanSection, TitleCandidate

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
    )
    if target_reader is not None:
        plan_kwargs["target_reader"] = target_reader
    return make_content_plan(**plan_kwargs)


def test_run_title_selection_replaces_disliked_with_llm_replacement(
    monkeypatch, patched_blog_job_store
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
            return {"titles": [{"title": "Better Title", "probability_of_success": 0.9}]}

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
    monkeypatch, patched_blog_job_store
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


def test_run_title_selection_propagates_cancelled_error(
    monkeypatch, patched_blog_job_store
) -> None:
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


def test_run_title_selection_swallows_generic_error(monkeypatch, patched_blog_job_store) -> None:
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


def test_wait_for_hitl_treats_missing_job_as_terminal(monkeypatch) -> None:
    """If the job vanishes mid-wait (get_blog_job -> None), the wait returns terminal
    immediately without sleeping, instead of polling a job that no longer exists."""
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "get_blog_job", lambda job_id: None)

    slept = {"n": 0}
    monkeypatch.setattr(v2.time, "sleep", lambda *_a, **_kw: slept.__setitem__("n", slept["n"] + 1))

    # Predicate reports "still waiting", but the job is gone: guard must exit terminal.
    result = v2._wait_for_hitl("job-x", lambda _job_id: True)
    assert result is True
    assert slept["n"] == 0


def test_wait_for_hitl_returns_false_when_wait_clears(monkeypatch) -> None:
    """When is_waiting flips to False (human responded), the helper returns False."""
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2, "get_blog_job", lambda job_id: {"status": "running"})
    monkeypatch.setattr(v2.time, "sleep", lambda *_a, **_kw: None)

    calls = {"n": 0}

    def _is_waiting(_job_id: str) -> bool:
        calls["n"] += 1
        return calls["n"] < 2  # waiting once, then cleared

    assert v2._wait_for_hitl("job-y", _is_waiting) is False


def test_wait_for_hitl_rides_out_transient_read_error(monkeypatch) -> None:
    """A transient job-store read failure is retried on the next poll rather than failing
    the whole job."""
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2.time, "sleep", lambda *_a, **_kw: None)

    reads = {"n": 0}

    def _get(_job_id: str):
        reads["n"] += 1
        if reads["n"] == 1:
            raise RuntimeError("job-store blip")
        return {"status": "cancelled"}  # terminal on the retry

    monkeypatch.setattr(v2, "get_blog_job", _get)
    # is_waiting stays True; the terminal status ends the loop after the retried read.
    assert v2._wait_for_hitl("job-z", lambda _job_id: True) is True
    assert reads["n"] == 2


def test_wait_for_hitl_reraises_after_persistent_read_errors(monkeypatch) -> None:
    """Consecutive read failures beyond the bound propagate — a persistent job-store outage
    still fails the job instead of looping forever."""
    import agent_implementations.blog_writing_process_v2 as v2

    monkeypatch.setattr(v2.time, "sleep", lambda *_a, **_kw: None)

    attempts = {"n": 0}

    def _boom(_job_id: str):
        attempts["n"] += 1
        raise RuntimeError("job-store down")

    monkeypatch.setattr(v2, "get_blog_job", _boom)
    with pytest.raises(RuntimeError, match="job-store down"):
        v2._wait_for_hitl("job-z", lambda _job_id: True)
    # One more attempt than the tolerated bound, then it gives up.
    assert attempts["n"] == v2.HITL_MAX_CONSECUTIVE_READ_ERRORS + 1
