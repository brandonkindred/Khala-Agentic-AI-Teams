"""Coverage for the cancel / delete / list endpoints and job-store wrappers
that the happy-path tests in ``test_api.py`` don't exercise.

These tests rely on the autouse ``fake_job_client`` fixture from the team
conftest, which routes every ``job_store._client()`` call through an
in-memory ``FakeJobServiceClient``.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from market_research_team import pipeline as mr_pipeline
from market_research_team.api.main import app
from market_research_team.models import HumanReview, ResearchMission, TeamTopology
from market_research_team.pipeline import run_market_research_background
from market_research_team.shared import job_store as js

client = TestClient(app)


def test_list_jobs_returns_running_filter_then_all(fake_job_client) -> None:
    running_id = str(uuid4())
    completed_id = str(uuid4())
    fake_job_client.create_job(running_id, status=js.JOB_STATUS_RUNNING)
    fake_job_client.create_job(completed_id, status=js.JOB_STATUS_COMPLETED)

    running_only = client.get("/market-research/jobs", params={"running_only": True})
    assert running_only.status_code == 200
    ids = {item["job_id"] for item in running_only.json()["jobs"]}
    assert running_id in ids
    assert completed_id not in ids

    all_jobs = client.get("/market-research/jobs")
    assert all_jobs.status_code == 200
    ids = {item["job_id"] for item in all_jobs.json()["jobs"]}
    assert running_id in ids
    assert completed_id in ids


def test_cancel_endpoint_pending_then_completed_then_unknown(fake_job_client) -> None:
    pending_id = str(uuid4())
    completed_id = str(uuid4())
    fake_job_client.create_job(pending_id, status=js.JOB_STATUS_PENDING)
    fake_job_client.create_job(completed_id, status=js.JOB_STATUS_COMPLETED)

    ok = client.post(f"/market-research/jobs/{pending_id}/cancel")
    assert ok.status_code == 200
    body = ok.json()
    assert body["success"] is True
    assert body["status"] == js.JOB_STATUS_CANCELLED

    bad = client.post(f"/market-research/jobs/{completed_id}/cancel")
    assert bad.status_code == 200
    body = bad.json()
    assert body["success"] is False
    assert js.JOB_STATUS_COMPLETED in body["message"]

    missing = client.post(f"/market-research/jobs/{uuid4()}/cancel")
    assert missing.status_code == 404


def test_delete_endpoint_present_then_missing(fake_job_client) -> None:
    job_id = str(uuid4())
    fake_job_client.create_job(job_id, status=js.JOB_STATUS_COMPLETED)

    ok = client.delete(f"/market-research/jobs/{job_id}")
    assert ok.status_code == 200
    assert ok.json() == {"job_id": job_id, "deleted": True}

    again = client.delete(f"/market-research/jobs/{job_id}")
    assert again.status_code == 404


def test_background_runner_returns_early_when_cancelled_before_start(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """Pre-cancelled jobs must not invoke the orchestrator."""

    called: dict[str, bool] = {"ran": False}

    class _ShouldNotRun:
        def run(self, *_: Any, **__: Any) -> Any:  # pragma: no cover
            called["ran"] = True
            raise AssertionError("Orchestrator should not run on cancelled job")

    monkeypatch.setattr(mr_pipeline, "MarketResearchOrchestrator", _ShouldNotRun)

    job_id = str(uuid4())
    fake_job_client.create_job(job_id, status=js.JOB_STATUS_CANCELLED)

    mission = ResearchMission(
        product_concept="Cancelled before start",
        target_users="x",
        business_goal="y",
        topology=TeamTopology.UNIFIED,
    )
    run_market_research_background(job_id, mission, HumanReview(approved=True))

    assert called["ran"] is False
    assert fake_job_client.get_job(job_id)["status"] == js.JOB_STATUS_CANCELLED


def test_background_runner_skips_completion_update_when_cancelled_mid_run(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """Cancellation after run() returns must skip the COMPLETED update."""

    job_id = str(uuid4())
    fake_job_client.create_job(job_id, status=js.JOB_STATUS_RUNNING)

    class _CancelDuringRun:
        def run(self, *_: Any, **__: Any) -> Any:
            fake_job_client.update_job(job_id, status=js.JOB_STATUS_CANCELLED)

            class _Result:
                @staticmethod
                def model_dump() -> dict[str, Any]:
                    return {"topology": "unified"}

            return _Result()

    monkeypatch.setattr(mr_pipeline, "MarketResearchOrchestrator", _CancelDuringRun)

    mission = ResearchMission(
        product_concept="Cancelled mid-run",
        target_users="x",
        business_goal="y",
        topology=TeamTopology.UNIFIED,
    )
    run_market_research_background(job_id, mission, HumanReview(approved=True))

    job = fake_job_client.get_job(job_id)
    assert job["status"] == js.JOB_STATUS_CANCELLED
    assert job.get("result") is None


def test_background_runner_skips_failure_update_when_cancelled_during_exception(
    monkeypatch: pytest.MonkeyPatch, fake_job_client
) -> None:
    """Cancellation between an orchestrator exception and the FAILED update
    must keep the job in cancelled state without overwriting it."""

    job_id = str(uuid4())
    fake_job_client.create_job(job_id, status=js.JOB_STATUS_RUNNING)

    class _CancelThenRaise:
        def run(self, *_: Any, **__: Any) -> Any:
            fake_job_client.update_job(job_id, status=js.JOB_STATUS_CANCELLED)
            raise RuntimeError("boom after cancel")

    monkeypatch.setattr(mr_pipeline, "MarketResearchOrchestrator", _CancelThenRaise)

    mission = ResearchMission(
        product_concept="Cancelled on exception",
        target_users="x",
        business_goal="y",
        topology=TeamTopology.UNIFIED,
    )
    run_market_research_background(job_id, mission, HumanReview(approved=True))

    job = fake_job_client.get_job(job_id)
    assert job["status"] == js.JOB_STATUS_CANCELLED
    assert job.get("error") is None
