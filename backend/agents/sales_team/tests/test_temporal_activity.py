"""Tests for the sales_team Temporal activity + workflow.

The activity delegates entirely to the existing ``_run_pipeline_job`` (which
owns the same RUNNING -> COMPLETED/FAILED job-store bookkeeping used by the
thread-dispatch path), then checks the resulting job status so a genuine
failure surfaces as a failed Temporal workflow rather than a
silently-"completed" one.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from sales_team.api import main as api_main
from sales_team.temporal import workflows as wf

_REQUEST = {
    "product_name": "ProductX",
    "value_proposition": "Save 20% on outbound time",
    "icp": {"industry": ["SaaS"]},
}


def test_activity_signature_takes_job_id_and_request():
    """Regression guard: the workflow passes (job_id, request); the activity
    must accept both so it can key job-store writes by job_id."""
    sig = inspect.signature(wf.run_pipeline_activity)
    assert list(sig.parameters) == ["job_id", "request"]


def test_activity_marks_job_completed_and_returns_job_id(monkeypatch, fake_job_client):
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    fake_job_client.create_job("job-ok", status="pending")

    class _StubOrch:
        def __init__(self, **_kw):
            pass

        def run(self, request, job_id, update_cb=None):
            return api_main.SalesPipelineResult(
                job_id=job_id, entry_stage=request.entry_stage, product_name=request.product_name
            )

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _StubOrch)

    result = wf.run_pipeline_activity("job-ok", _REQUEST)

    assert result == {"job_id": "job-ok"}
    job = fake_job_client.get_job("job-ok")
    assert job["status"] == "completed"
    assert job["result"]["job_id"] == "job-ok"


def test_activity_raises_when_job_ends_failed(monkeypatch, fake_job_client):
    """A genuine failure marks the job FAILED (via _run_pipeline_job) and the
    activity re-raises so Temporal sees a failed workflow."""
    monkeypatch.setattr(api_main, "_job_manager", fake_job_client)
    fake_job_client.create_job("job-boom", status="pending")

    class _RaisingOrch:
        def __init__(self, **_kw):
            pass

        def run(self, request, job_id, update_cb=None):
            raise RuntimeError("orchestrator exploded")

    monkeypatch.setattr(api_main, "SalesPodOrchestrator", _RaisingOrch)

    with pytest.raises(RuntimeError, match="orchestrator exploded"):
        wf.run_pipeline_activity("job-boom", _REQUEST)

    job = fake_job_client.get_job("job-boom")
    assert job["status"] == "failed"
    assert "orchestrator exploded" in (job.get("error") or "")


def test_workflow_run_delegates_to_activity(monkeypatch):
    """``SalesWorkflow.run`` forwards (job_id, request) to the activity via
    ``execute_activity`` with a bounded retry policy."""
    captured: dict = {}

    async def _fake_execute_activity(fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = kwargs.get("args")
        captured["retry_policy"] = kwargs.get("retry_policy")
        return {"job_id": "job-wf"}

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    out = asyncio.run(wf.SalesWorkflow().run("job-wf", _REQUEST))

    assert out == {"job_id": "job-wf"}
    assert captured["fn"] is wf.run_pipeline_activity
    assert captured["args"] == ["job-wf", _REQUEST]
    # Retries are explicitly capped at a single attempt (non-idempotent LLM
    # pipeline; the llm_service layer already handles transient failover).
    assert captured["retry_policy"].maximum_attempts == 1
