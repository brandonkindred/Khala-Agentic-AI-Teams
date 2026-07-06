"""Tests for the startup-advisor Temporal activity + workflow.

The activity owns the same job-store bookkeeping the thread path performs via
``_run_advisor_core``: RUNNING → COMPLETED with the serialized conversation
state on success, FAILED + re-raise on error. These tests pin that contract
against the in-memory fake job client (installed by the team ``conftest.py``
for non-integration tests).
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from startup_advisor.api import main as api_main
from startup_advisor.shared.job_store import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    create_job,
    get_job,
)
from startup_advisor.temporal import workflows as wf


def test_activity_signature_takes_job_id_and_message():
    """Regression guard: the workflow passes (job_id, message); the activity
    must accept both so it can key job-store writes by job_id."""
    sig = inspect.signature(wf.run_pipeline_activity)
    assert list(sig.parameters) == ["job_id", "message"]


def test_activity_marks_job_completed_with_result(monkeypatch):
    canned = api_main.ConversationStateResponse(
        conversation_id="conv-1",
        messages=[],
        context={},
        artifacts=[],
        suggested_questions=[],
    )
    monkeypatch.setattr(api_main, "_process_advisor_message", lambda message: canned)
    create_job("job-ok", message="hello")

    result = wf.run_pipeline_activity("job-ok", "hello")

    assert result == {"job_id": "job-ok"}
    job = get_job("job-ok")
    assert job["status"] == JOB_STATUS_COMPLETED
    assert job["result"]["conversation_id"] == "conv-1"


def test_activity_marks_job_failed_and_reraises_on_exception(monkeypatch):
    """A genuine failure marks the job FAILED and re-raises so Temporal sees a
    failed workflow (not a silently-completed one)."""

    def _boom(_message):
        raise RuntimeError("advisor exploded")

    monkeypatch.setattr(api_main, "_process_advisor_message", _boom)
    create_job("job-boom", message="hello")

    with pytest.raises(RuntimeError, match="advisor exploded"):
        wf.run_pipeline_activity("job-boom", "hello")

    job = get_job("job-boom")
    assert job["status"] == JOB_STATUS_FAILED
    assert "advisor exploded" in (job.get("error") or "")


def test_workflow_run_delegates_to_activity(monkeypatch):
    """``StartupAdvisorWorkflow.run`` forwards (job_id, message) to the activity
    via ``execute_activity`` with a bounded retry policy."""
    captured: dict = {}

    async def _fake_execute_activity(fn, *args, **kwargs):
        captured["fn"] = fn
        captured["args"] = kwargs.get("args")
        captured["task_queue"] = kwargs.get("task_queue")
        captured["retry_policy"] = kwargs.get("retry_policy")
        return {"job_id": "job-wf"}

    monkeypatch.setattr(wf.workflow, "execute_activity", _fake_execute_activity)

    out = asyncio.run(wf.StartupAdvisorWorkflow().run("job-wf", "hello"))

    assert out == {"job_id": "job-wf"}
    assert captured["fn"] is wf.run_pipeline_activity
    assert captured["args"] == ["job-wf", "hello"]
    assert captured["task_queue"] == wf.TASK_QUEUE
    # Retries are explicitly capped at a single attempt: _run_advisor_core
    # appends the user message as a non-idempotent side effect.
    assert captured["retry_policy"].maximum_attempts == 1
