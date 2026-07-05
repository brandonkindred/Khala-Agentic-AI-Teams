"""Tests for the road-trip Temporal activity + workflow.

The activity owns the same job-store bookkeeping the thread path performs in
``pipeline.run_plan_background``: RUNNING → COMPLETED with the itinerary result
on success, FAILED + re-raise on error. These tests pin that contract against
the in-memory fake job client (installed by the team ``conftest.py`` for
non-integration tests).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from road_trip_planning_team import pipeline as rtp_pipeline
from road_trip_planning_team.models import TripItinerary
from road_trip_planning_team.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    create_job,
    get_job,
)
from road_trip_planning_team.temporal import workflows as wf


def test_activity_signature_takes_job_id_and_request():
    """Regression guard: the workflow passes (job_id, request); the activity
    must accept both so it can key job-store writes by job_id."""
    sig = inspect.signature(wf.run_pipeline_activity)
    assert list(sig.parameters) == ["job_id", "request"]


def test_activity_marks_job_completed_with_result(monkeypatch, sample_trip_body):
    canned = TripItinerary(title="Test Trip", overview="ok", total_days=3)
    monkeypatch.setattr(rtp_pipeline, "run_pipeline", lambda body: canned)
    create_job("job-ok", request=sample_trip_body)

    result = wf.run_pipeline_activity("job-ok", sample_trip_body)

    assert result == {"job_id": "job-ok"}
    job = get_job("job-ok")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["title"] == "Test Trip"


def test_activity_marks_job_failed_and_reraises_on_exception(monkeypatch, sample_trip_body):
    """A genuine failure marks the job FAILED and re-raises so Temporal sees a
    failed workflow (not a silently-completed one)."""

    def _boom(_body):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(rtp_pipeline, "run_pipeline", _boom)
    create_job("job-boom", request=sample_trip_body)

    with pytest.raises(RuntimeError, match="pipeline exploded"):
        wf.run_pipeline_activity("job-boom", sample_trip_body)

    job = get_job("job-boom")
    assert job["status"] == JOB_STATUS_FAILED
    assert "pipeline exploded" in (job.get("error") or "")


def test_workflow_run_delegates_to_activity(monkeypatch, sample_trip_body):
    """``RoadTripWorkflow.run`` forwards (job_id, request) to the activity via
    ``execute_activity`` with a bounded retry policy."""
    captured: dict = {}

    async def _fake_execute_activity(fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = kwargs.get("args")
        captured["task_queue"] = kwargs.get("task_queue")
        captured["retry_policy"] = kwargs.get("retry_policy")
        return {"job_id": "job-wf"}

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    out = asyncio.run(wf.RoadTripWorkflow().run("job-wf", sample_trip_body))

    assert out == {"job_id": "job-wf"}
    assert captured["fn"] is wf.run_pipeline_activity
    assert captured["args"] == ["job-wf", sample_trip_body]
    assert captured["task_queue"] == wf.TASK_QUEUE
    # Retries are explicitly capped at a single attempt (non-idempotent LLM
    # pipeline; the llm_service layer already handles transient failover).
    assert captured["retry_policy"].maximum_attempts == 1
