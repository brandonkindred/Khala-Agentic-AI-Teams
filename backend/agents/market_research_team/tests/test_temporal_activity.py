"""Tests for the market_research Temporal activity + workflow.

The activity owns the same job-store bookkeeping the thread path performs
in ``api.main._run_market_research_background``: RUNNING → COMPLETED with the
orchestrator result on success, FAILED on error, and a no-op when the job was
cancelled. These tests pin that contract against the in-memory
``fake_job_client`` (autouse-patched in ``tests/conftest.py``).
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import MagicMock

from market_research_team.shared.job_store import (
    JOB_STATUS_CANCELLED,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    create_job,
    get_job,
    update_job,
)
from market_research_team.temporal import workflows as wf

_REQUEST = {
    "product_concept": "Interview analysis assistant",
    "target_users": "startup founders",
    "business_goal": "validate demand faster",
    "topology": "unified",
    "transcripts": ["Users want confidence before building features."],
    "human_approved": True,
}


def test_activity_signature_takes_job_id_and_request():
    """Regression guard: the workflow passes (job_id, request); the activity
    must accept both so it can key job-store writes by job_id."""
    sig = inspect.signature(wf.run_pipeline_activity)
    params = list(sig.parameters)
    assert params == ["job_id", "request"]


def test_activity_marks_job_completed_with_result():
    create_job("job-ok", request=_REQUEST, product_concept=_REQUEST["product_concept"])

    result = wf.run_pipeline_activity("job-ok", _REQUEST)

    assert result == {"job_id": "job-ok"}
    job = get_job("job-ok")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert isinstance(job["result"], dict)
    assert job["result"]["topology"] == "unified"


def test_activity_marks_job_failed_on_exception(monkeypatch):
    class _BoomOrchestrator:
        def run(self, *_args, **_kwargs):
            raise RuntimeError("orchestrator exploded")

    monkeypatch.setattr(
        "market_research_team.orchestrator.MarketResearchOrchestrator", _BoomOrchestrator
    )
    create_job("job-boom", request=_REQUEST)

    result = wf.run_pipeline_activity("job-boom", _REQUEST)

    assert result == {"job_id": "job-boom"}
    job = get_job("job-boom")
    assert job["status"] == JOB_STATUS_FAILED
    assert "orchestrator exploded" in (job.get("error") or "")


def test_activity_short_circuits_when_cancelled_before_run(monkeypatch):
    ran = MagicMock()

    class _TrackingOrchestrator:
        def run(self, *_args, **_kwargs):  # pragma: no cover - must not run
            ran()
            return MagicMock()

    monkeypatch.setattr(
        "market_research_team.orchestrator.MarketResearchOrchestrator", _TrackingOrchestrator
    )
    create_job("job-cancel", request=_REQUEST)
    update_job("job-cancel", status=JOB_STATUS_CANCELLED)

    result = wf.run_pipeline_activity("job-cancel", _REQUEST)

    assert result == {"job_id": "job-cancel"}
    ran.assert_not_called()
    assert get_job("job-cancel")["status"] == JOB_STATUS_CANCELLED


def test_activity_does_not_overwrite_when_cancelled_mid_run(monkeypatch):
    """A cancel that lands while the orchestrator is running must not be
    clobbered by a COMPLETED write."""

    class _CancellingOrchestrator:
        def run(self, *_args, **_kwargs):
            update_job("job-midcancel", status=JOB_STATUS_CANCELLED)
            return MagicMock()

    monkeypatch.setattr(
        "market_research_team.orchestrator.MarketResearchOrchestrator", _CancellingOrchestrator
    )
    create_job("job-midcancel", request=_REQUEST)

    result = wf.run_pipeline_activity("job-midcancel", _REQUEST)

    assert result == {"job_id": "job-midcancel"}
    job = get_job("job-midcancel")
    assert job["status"] == JOB_STATUS_CANCELLED
    assert "result" not in job


def test_workflow_run_delegates_to_activity(monkeypatch):
    """``MarketResearchWorkflow.run`` forwards (job_id, request) to the
    activity via ``execute_activity``."""
    captured: dict = {}

    async def _fake_execute_activity(fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = kwargs.get("args")
        return {"job_id": "job-wf"}

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    out = asyncio.run(wf.MarketResearchWorkflow().run("job-wf", {"product_concept": "x"}))

    assert out == {"job_id": "job-wf"}
    assert captured["fn"] is wf.run_pipeline_activity
    assert captured["args"] == ["job-wf", {"product_concept": "x"}]
