"""Tests for the ``POST /plan`` Temporal-vs-thread dispatch branch.

With ``TEMPORAL_ADDRESS`` unset ``is_temporal_enabled()`` is False, so the
existing ``test_api.py`` cases already cover the thread path end-to-end. These
tests cover the Temporal branch (patched enabled) and the dispatch-failure
handling, without needing a running Temporal server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from road_trip_planning_team import pipeline as rtp_pipeline
from road_trip_planning_team.api import main as api_main
from road_trip_planning_team.shared.job_store import JOB_STATUS_FAILED


@pytest.fixture
def client():
    with TestClient(api_main.app) as c:
        yield c


def test_plan_dispatches_to_temporal_when_enabled(client, monkeypatch, sample_trip_body):
    # The dispatch helper imports both names lazily from their live modules.
    # Patch via string paths so the patch targets whatever module object
    # sys.modules currently holds.
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    captured: dict = {}
    monkeypatch.setattr(
        "road_trip_planning_team.temporal.start_workflow.start_road_trip_workflow",
        lambda job_id, request: captured.update(job_id=job_id, request=request),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    response = client.post("/plan", json=sample_trip_body)

    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    assert captured["job_id"] == job_id
    assert captured["request"]["trip"]["start_location"] == "San Francisco, CA"


def test_plan_marks_job_failed_when_dispatch_raises(
    client, monkeypatch, sample_trip_body, fake_job_client
):
    """A dispatch failure (e.g. Temporal worker client never connected) must
    leave the job in a terminal FAILED state, not orphaned in PENDING."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, request):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr(
        "road_trip_planning_team.temporal.start_workflow.start_road_trip_workflow", _boom
    )

    response = client.post("/plan", json=sample_trip_body)

    assert response.status_code == 500
    assert "Failed to start road trip planning run" in response.json().get("detail", "")
    # The freshly-created job must reach a terminal FAILED state, not stay
    # orphaned in PENDING — assert the store row, not just the HTTP body.
    jobs = fake_job_client.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == JOB_STATUS_FAILED
    assert "Dispatch failed" in (jobs[0].get("error") or "")


def test_dispatch_helper_returns_thread_label_when_disabled(monkeypatch, sample_plan_request):
    """Direct unit check of the helper's thread fallback and its label."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: False)

    started: dict = {}

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)

    body = sample_plan_request
    label = api_main._dispatch_plan_run("job-thread", body)

    assert label == "thread"
    assert started["started"] is True
    assert started["daemon"] is True
    assert started["target"] is rtp_pipeline.run_plan_background
    assert started["args"] == ("job-thread", body)
