"""Tests for the nutrition Temporal activities.

Each ``@activity.defn`` function is exercised as a plain function (no Temporal
server). They own the same job-store bookkeeping the thread path performs via
``pipeline.run_*_background``: RUNNING → COMPLETED on success, FAILED + re-raise
on error (``ValueError`` → ``not_found``), early-return on cancel. These tests
pin that contract against the in-memory fake job client (installed by the team
``conftest.py`` for non-integration tests).
"""

from __future__ import annotations

import inspect

import pytest

from nutrition_meal_planning_team import pipeline
from nutrition_meal_planning_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    create_job,
    get_job,
    update_job,
)
from nutrition_meal_planning_team.temporal import workflows as wf
from nutrition_meal_planning_team.tests._fakes import patch_orch

# activity fn + the second positional arg the workflow passes it.
_CASES = {
    "nutrition_plan": (wf.run_nutrition_plan_activity, {"client_id": "client-1"}),
    "regenerate": (wf.run_nutrition_regenerate_activity, "client-1"),
    "meal_plan": (wf.run_meal_plan_activity, {"client_id": "client-1"}),
}


@pytest.fixture(params=list(_CASES), ids=list(_CASES))
def activity_case(request):
    return _CASES[request.param]


def test_activity_signatures():
    """The workflows pass (job_id, <second arg>); each activity must accept the
    matching parameters so it can key job-store writes by job_id."""
    assert list(inspect.signature(wf.run_nutrition_plan_activity).parameters) == [
        "job_id",
        "request",
    ]
    assert list(inspect.signature(wf.run_nutrition_regenerate_activity).parameters) == [
        "job_id",
        "client_id",
    ]
    assert list(inspect.signature(wf.run_meal_plan_activity).parameters) == ["job_id", "request"]


def test_activity_marks_job_completed_with_result(monkeypatch, activity_case):
    fn, arg = activity_case
    patch_orch(monkeypatch)
    create_job("job-ok")

    result = fn("job-ok", arg)

    assert result == {"job_id": "job-ok"}
    job = get_job("job-ok")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["client_id"] == "client-1"


def test_activity_value_error_marks_failed_not_found_and_reraises(monkeypatch, activity_case):
    fn, arg = activity_case
    patch_orch(monkeypatch, exc=ValueError("Profile not found"))
    create_job("job-404")

    with pytest.raises(ValueError, match="Profile not found"):
        fn("job-404", arg)

    job = get_job("job-404")
    assert job["status"] == JOB_STATUS_FAILED
    assert job["not_found"] is True


def test_activity_generic_error_marks_failed_and_reraises(monkeypatch, activity_case):
    fn, arg = activity_case
    patch_orch(monkeypatch, exc=RuntimeError("pipeline exploded"))
    create_job("job-boom")

    with pytest.raises(RuntimeError, match="pipeline exploded"):
        fn("job-boom", arg)

    job = get_job("job-boom")
    assert job["status"] == JOB_STATUS_FAILED
    assert "pipeline exploded" in (job.get("error") or "")
    # A generic failure is not a not-found; that flag is reserved for ValueError.
    assert job.get("not_found") is None


def test_activity_returns_early_when_cancelled(monkeypatch, activity_case):
    fn, arg = activity_case
    # Orchestrator would succeed if it ran — the pre-run cancel guard must skip it.
    patch_orch(monkeypatch)
    create_job("job-cancel")
    update_job("job-cancel", status=JOB_STATUS_CANCELLED)

    result = fn("job-cancel", arg)

    assert result == {"job_id": "job-cancel"}
    # The row is left in its cancelled (terminal) state — no RUNNING/COMPLETED write.
    assert get_job("job-cancel")["status"] == JOB_STATUS_CANCELLED


@pytest.mark.parametrize(
    "fn, arg, core_attr",
    [
        (wf.run_nutrition_plan_activity, {"client_id": "c"}, "run_nutrition_plan_core"),
        (wf.run_nutrition_regenerate_activity, "c", "run_regenerate_core"),
        (wf.run_meal_plan_activity, {"client_id": "c"}, "run_meal_plan_core"),
    ],
)
def test_activity_swallows_when_cancelled_mid_exception(monkeypatch, fn, arg, core_attr):
    """If the pipeline raises but the job was cancelled meanwhile, the activity
    returns (cancelled is terminal) instead of re-raising / marking FAILED."""

    def _raise(*_a, **_k):
        raise RuntimeError("boom after cancel")

    monkeypatch.setattr(pipeline, core_attr, _raise)
    create_job("job-cx")
    update_job("job-cx", status=JOB_STATUS_CANCELLED)

    result = fn("job-cx", arg)

    assert result == {"job_id": "job-cx"}
    # Left cancelled — no FAILED overwrite.
    assert get_job("job-cx")["status"] == JOB_STATUS_CANCELLED


def test_activity_heartbeats_during_core_run(monkeypatch):
    """The activity wraps the core in a BackgroundHeartbeat driving
    ``activity.heartbeat`` on the team interval, so a dead/hung worker is caught
    by the workflow's heartbeat_timeout."""
    from temporalio import activity

    import shared_concurrency

    entered: dict = {"count": 0}

    class _FakeHeartbeat:
        def __init__(self, beat, interval_s, **_kwargs):
            entered["beat"] = beat
            entered["interval"] = interval_s

        def __enter__(self):
            entered["count"] += 1
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(shared_concurrency, "BackgroundHeartbeat", _FakeHeartbeat)
    patch_orch(monkeypatch)
    create_job("job-hb")

    result = wf.run_meal_plan_activity("job-hb", {"client_id": "client-1"})

    assert result == {"job_id": "job-hb"}
    assert entered["count"] == 1
    assert entered["beat"] is activity.heartbeat
    assert entered["interval"] == wf.HEARTBEAT_INTERVAL_S
    assert get_job("job-hb")["status"] == JOB_STATUS_COMPLETED


def test_activity_reraises_original_error_when_mark_job_failed_raises(monkeypatch):
    """If recording the failure itself raises (e.g. job store down), the ORIGINAL
    pipeline error must still propagate — not be masked by the bookkeeping error."""

    def _core_boom(*_a, **_k):
        raise RuntimeError("original pipeline error")

    def _mark_boom(_job_id, _exc):
        raise RuntimeError("job store unavailable")

    monkeypatch.setattr(pipeline, "run_meal_plan_core", _core_boom)
    monkeypatch.setattr(pipeline, "mark_job_failed", _mark_boom)
    create_job("job-mm")

    with pytest.raises(RuntimeError, match="original pipeline error"):
        wf.run_meal_plan_activity("job-mm", {"client_id": "c"})
