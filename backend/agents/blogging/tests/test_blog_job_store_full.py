"""Comprehensive unit tests for blog_job_store.

Covers helper functions not exercised by ``test_blog_job_store.py``:
create/start/complete/fail, title selection, story chat history, Q&A,
draft review, and guideline updates. Backed by the in-memory
``FakeJobServiceClient`` from ``backend/conftest.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path


def _make_job(cache_dir: Path) -> str:
    from agents.blogging.shared.blog_job_store import create_blog_job

    job_id = str(uuid.uuid4())
    create_blog_job(
        job_id,
        "Brief text",
        audience="general",
        tone_or_purpose="inform",
        work_dir=str(cache_dir / "work"),
        job_type="blog",
        cache_dir=cache_dir,
    )
    return job_id


def test_create_then_start_then_complete(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        complete_blog_job,
        get_blog_job,
        start_blog_job,
    )

    job_id = _make_job(tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["status"] == "pending"
    assert job["brief"] == "Brief text"

    start_blog_job(job_id, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["status"] == "running"
    assert job["started_at"]

    complete_blog_job(
        job_id,
        title_choices=[{"title": "A"}],
        outline="# Outline",
        draft_preview="preview",
        content_plan_summary="summary",
        planning_iterations_used=2,
        parse_retry_count=1,
        planning_wall_ms_total=12.3,
        cache_dir=tmp_path,
    )
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["status"] == "completed"
    assert job["title_choices"] == [{"title": "A"}]
    assert job["outline"] == "# Outline"
    assert job["draft_preview"] == "preview"
    assert job["content_plan_summary"] == "summary"
    assert job["planning_iterations_used"] == 2
    assert job["parse_retry_count"] == 1
    assert job["planning_wall_ms_total"] == 12.3
    assert job["progress"] == 100


def test_complete_with_needs_review_status(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import complete_blog_job, get_blog_job

    job_id = _make_job(tmp_path)
    complete_blog_job(job_id, status="needs_human_review", cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["status"] == "needs_human_review"
    assert job["status_text"] == "Needs human review"


def test_fail_blog_job_records_error(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import fail_blog_job, get_blog_job

    job_id = _make_job(tmp_path)
    fail_blog_job(
        job_id,
        error="LLM down",
        failed_phase="planning",
        planning_failure_reason="timeout",
        cache_dir=tmp_path,
    )
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["status"] == "failed"
    assert job["error"] == "LLM down"
    assert job["failed_phase"] == "planning"
    assert job["planning_failure_reason"] == "timeout"


def test_list_blog_jobs_filters_running_only(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        complete_blog_job,
        list_blog_jobs,
        start_blog_job,
    )

    a = _make_job(tmp_path)
    b = _make_job(tmp_path)
    start_blog_job(a, cache_dir=tmp_path)
    complete_blog_job(b, cache_dir=tmp_path)

    all_jobs = list_blog_jobs(cache_dir=tmp_path)
    ids = {j["job_id"] for j in all_jobs}
    assert {a, b}.issubset(ids)

    running = list_blog_jobs(cache_dir=tmp_path, running_only=True)
    assert any(j["job_id"] == a for j in running)
    assert all(j["job_id"] != b for j in running)


def test_reset_blog_job_clears_progress(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        complete_blog_job,
        get_blog_job,
        reset_blog_job,
        update_blog_job,
    )

    job_id = _make_job(tmp_path)
    complete_blog_job(job_id, outline="X", cache_dir=tmp_path)
    update_blog_job(job_id, research_sources_count=7, cache_dir=tmp_path)
    reset_blog_job(job_id, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["status"] == "pending"
    assert job["progress"] == 0
    assert job["outline"] is None
    # A restarted job must not keep reporting the previous run's research
    # source count while the new research step is pending/in-flight.
    assert job["research_sources_count"] == 0


def test_approve_unapprove_blog_job(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        approve_blog_job,
        get_blog_job,
        unapprove_blog_job,
    )

    job_id = _make_job(tmp_path)
    approve_blog_job(job_id, approved_by="brandon", cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["approved_at"]
    assert job["approved_by"] == "brandon"

    unapprove_blog_job(job_id, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["approved_at"] is None
    assert job["approved_by"] is None


def test_title_selection_and_love_rating(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        get_blog_job,
        is_waiting_for_title_selection,
        submit_title_ratings,
        submit_title_selection,
        update_blog_job,
    )

    job_id = _make_job(tmp_path)
    update_blog_job(job_id, waiting_for_title_selection=True, cache_dir=tmp_path)
    assert is_waiting_for_title_selection(job_id, cache_dir=tmp_path) is True

    submit_title_selection(job_id, "Picked", cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["selected_title"] == "Picked"
    assert job["waiting_for_title_selection"] is False
    assert is_waiting_for_title_selection(job_id, cache_dir=tmp_path) is False

    # Love rating clears wait
    update_blog_job(job_id, waiting_for_title_selection=True, cache_dir=tmp_path)
    submit_title_ratings(
        job_id,
        [{"title": "Wow", "rating": "love"}, {"title": "Eh", "rating": "dislike"}],
        cache_dir=tmp_path,
    )
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["selected_title"] == "Wow"
    assert job["waiting_for_title_selection"] is False


def test_title_ratings_without_love_stays_paused(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        clear_pending_title_feedback,
        get_blog_job,
        get_pending_title_feedback,
        submit_title_ratings,
        update_blog_job,
    )

    job_id = _make_job(tmp_path)
    update_blog_job(job_id, waiting_for_title_selection=True, cache_dir=tmp_path)
    ratings = [{"title": "OK", "rating": "like"}]
    submit_title_ratings(job_id, ratings, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["waiting_for_title_selection"] is True
    assert job.get("pending_title_feedback") == ratings

    assert get_pending_title_feedback(job_id, cache_dir=tmp_path) == ratings
    assert get_pending_title_feedback("nonexistent", cache_dir=tmp_path) is None

    clear_pending_title_feedback(job_id, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job.get("pending_title_feedback") is None


def test_story_chat_history(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        add_story_agent_message,
        complete_story_elicitation,
        get_blog_job,
        is_waiting_for_story_input,
        skip_current_story_gap,
        submit_story_user_message,
        update_blog_job,
    )

    job_id = _make_job(tmp_path)
    update_blog_job(job_id, current_gap_round=2, cache_dir=tmp_path)

    add_story_agent_message(job_id, "What happened?", gap_index=0, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["waiting_for_story_input"] is True
    assert is_waiting_for_story_input(job_id, cache_dir=tmp_path) is True
    assert job["story_chat_history"][-1]["role"] == "agent"
    assert job["story_chat_history"][-1]["gap_round"] == 2

    submit_story_user_message(job_id, "It was raining.", cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["waiting_for_story_input"] is False
    assert job["story_chat_history"][-1]["role"] == "user"

    skip_current_story_gap(job_id, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["current_story_gap_index"] == 1

    complete_story_elicitation(job_id, ["story-1", "story-2"], cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["elicited_stories"] == ["story-1", "story-2"]


def test_is_waiting_for_story_input_missing_job(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import is_waiting_for_story_input

    assert is_waiting_for_story_input("missing", cache_dir=tmp_path) is False


def test_qa_pause_resume(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        add_blog_pending_questions,
        get_blog_job,
        is_waiting_for_blog_answers,
        submit_blog_answers,
    )

    job_id = _make_job(tmp_path)
    qs = [{"id": "q1", "text": "What is the target audience?"}]
    add_blog_pending_questions(job_id, qs, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["pending_questions"] == qs
    assert job["waiting_for_answers"] is True
    assert is_waiting_for_blog_answers(job_id, cache_dir=tmp_path) is True
    assert is_waiting_for_blog_answers("missing", cache_dir=tmp_path) is False

    submit_blog_answers(job_id, [{"id": "q1", "answer": "Engineers"}], cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["waiting_for_answers"] is False
    assert job["pending_questions"] == []
    assert job["submitted_answers"][-1]["answer"] == "Engineers"


def test_draft_feedback_cycle(tmp_path: Path) -> None:
    from agents.blogging.shared.blog_job_store import (
        get_blog_job,
        get_user_draft_feedback,
        is_waiting_for_draft_feedback,
        record_guideline_updates,
        request_draft_feedback,
        submit_draft_feedback,
    )

    job_id = _make_job(tmp_path)
    assert is_waiting_for_draft_feedback(job_id, cache_dir=tmp_path) is False
    assert get_user_draft_feedback(job_id, cache_dir=tmp_path) is None

    request_draft_feedback(
        job_id,
        "draft text",
        revision=1,
        uncertainty_questions=[{"q": "x"}],
        escalation_summary="some summary",
        cache_dir=tmp_path,
    )
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["waiting_for_draft_feedback"] is True
    assert job["draft_for_review"] == "draft text"
    assert job["draft_review_revision"] == 1
    assert job["draft_review_questions"] == [{"q": "x"}]
    assert job["draft_escalation_summary"] == "some summary"

    submit_draft_feedback(job_id, "looks good", approved=True, cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["waiting_for_draft_feedback"] is False
    assert job["user_draft_feedback"] == {"feedback": "looks good", "approved": True}
    assert job["draft_review_questions"] == []
    assert job["draft_escalation_summary"] is None

    record_guideline_updates(job_id, [{"rule": "no exclamation marks"}], cache_dir=tmp_path)
    job = get_blog_job(job_id, cache_dir=tmp_path)
    assert job["guideline_updates_applied"][-1]["rule"] == "no exclamation marks"


def test_medium_stats_run_dir_uses_custom_root(tmp_path: Path, monkeypatch) -> None:
    from agents.blogging.shared.blog_job_store import medium_stats_run_dir

    custom = tmp_path / "custom-root"
    monkeypatch.setenv("BLOGGING_MEDIUM_STATS_ROOT", str(custom))

    job_id = "abc-123"
    out = medium_stats_run_dir(job_id, cache_dir=tmp_path)
    assert out == custom.resolve() / job_id
    assert out.is_dir()


def test_medium_stats_run_dir_default(tmp_path: Path, monkeypatch) -> None:
    from agents.blogging.shared.blog_job_store import medium_stats_run_dir

    monkeypatch.delenv("BLOGGING_MEDIUM_STATS_ROOT", raising=False)
    out = medium_stats_run_dir("xyz", cache_dir=tmp_path)
    expected = tmp_path.resolve() / "blogging_team" / "medium_stats_runs" / "xyz"
    assert out == expected
    assert out.is_dir()


def test_stale_monitor_stop_safe_when_not_started(tmp_path: Path, monkeypatch) -> None:
    """stop_blog_stale_monitor is a no-op when the monitor was never started."""
    from agents.blogging.shared import blog_job_store as bjs

    # Replace the global so we can assert the no-op path
    monkeypatch.setattr(bjs, "_blog_stale_monitor_stop", None)
    bjs.stop_blog_stale_monitor()  # must not raise


def test_mark_all_running_jobs_failed_swallows_exceptions(tmp_path: Path, monkeypatch) -> None:
    """Failures in the underlying client are swallowed with a logger warning."""

    class _Boom:
        def mark_all_active_jobs_interrupted(self, _reason: str) -> None:
            raise RuntimeError("nope")

    from agents.blogging.shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: _Boom())
    # Must not raise
    bjs.mark_all_running_jobs_failed("server down", cache_dir=tmp_path)
