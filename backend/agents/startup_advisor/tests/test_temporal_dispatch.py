"""Tests for the ``POST /conversation/messages`` Temporal-vs-thread dispatch branch.

With ``TEMPORAL_ADDRESS`` unset ``is_temporal_enabled()`` is False, so the
thread path already runs unchanged in normal operation. These tests cover the
Temporal branch (patched enabled) and the dispatch-failure handling, without
needing a running Temporal server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from startup_advisor import pipeline
from startup_advisor.api import main as api_main
from startup_advisor.shared.job_store import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, create_job


@pytest.fixture
def client():
    with TestClient(api_main.app) as c:
        yield c


def test_send_message_dispatches_to_temporal_when_enabled(client, monkeypatch):
    # The dispatch helper imports both names lazily from their live modules.
    # Patch via string paths so the patch targets whatever module object
    # sys.modules currently holds.
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    captured: dict = {}
    monkeypatch.setattr(
        "startup_advisor.temporal.start_workflow.start_startup_advisor_workflow",
        lambda job_id, message: captured.update(job_id=job_id, message=message),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    response = client.post("/conversation/messages", json={"message": "Hello there"})

    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    assert captured["job_id"] == job_id
    assert captured["message"] == "Hello there"


def test_send_message_marks_job_failed_when_dispatch_raises(client, monkeypatch, fake_job_client):
    """A dispatch failure (e.g. Temporal worker client never connected) must
    leave the job in a terminal FAILED state, not orphaned in PENDING."""
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: True)

    def _boom(job_id, message):
        raise RuntimeError("worker client not available")

    monkeypatch.setattr(
        "startup_advisor.temporal.start_workflow.start_startup_advisor_workflow", _boom
    )

    response = client.post("/conversation/messages", json={"message": "Hello there"})

    assert response.status_code == 500
    assert "Failed to start startup advisor run" in response.json().get("detail", "")
    # The freshly-created job must reach a terminal FAILED state, not stay
    # orphaned in PENDING — assert the store row, not just the HTTP body.
    jobs = fake_job_client.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == JOB_STATUS_FAILED
    assert "Dispatch failed" in (jobs[0].get("error") or "")


def test_dispatch_helper_returns_thread_label_when_disabled(monkeypatch):
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

    label = api_main._dispatch_advisor_message("job-thread", "hi")

    assert label == "thread"
    assert started["started"] is True
    assert started["daemon"] is True
    assert started["target"] is api_main._run_advisor_message_background
    assert started["args"] == ("job-thread", "hi")


def test_background_swallows_update_job_failure_on_mark_failed(monkeypatch):
    """A job-store outage while recording the FAILED status must not kill the
    background thread with an unhandled exception (it has no supervisor)."""

    def _boom_process(_message):
        raise RuntimeError("advisor exploded")

    def _boom_update_job(*_a, **_k):
        # RUNNING is set (via pipeline's own update_job reference) before
        # process_advisor_message runs; only make the FAILED write (from
        # api_main's except block) blow up, isolating the scenario finding 3
        # is about.
        if _k.get("status") == JOB_STATUS_FAILED:
            raise RuntimeError("job store unreachable")

    monkeypatch.setattr(pipeline, "process_advisor_message", _boom_process)
    monkeypatch.setattr(api_main, "update_job", _boom_update_job)
    create_job("job-double-fail", message="hi")

    # Must not raise despite both the pipeline and the failure-reporting call
    # raising.
    api_main._run_advisor_message_background("job-double-fail", "hi")


def test_send_message_endpoint_thread_path_end_to_end(client, monkeypatch):
    """End-to-end check (via TestClient) of the default, most-used code path:
    Temporal disabled, thread fallback, through to a terminal job status.

    Runs the background function synchronously (via a thread stub that calls
    the target immediately) so the test is deterministic, and stubs
    ``process_advisor_message`` so no real LLM/agent call is made.
    """
    monkeypatch.setattr("shared_temporal.is_temporal_enabled", lambda: False)

    canned = pipeline.ConversationStateResponse(
        conversation_id="conv-e2e",
        messages=[],
        context={},
        artifacts=[],
        suggested_questions=[],
    )
    monkeypatch.setattr(pipeline, "process_advisor_message", lambda message: canned)

    class _SyncThread:
        def __init__(self, *, target, args, daemon):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(api_main.threading, "Thread", _SyncThread)

    response = client.post("/conversation/messages", json={"message": "Hello there"})

    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]

    status_response = client.get(f"/conversation/messages/status/{job_id}")
    assert status_response.status_code == 200
    status_body = status_response.json()
    assert status_body["status"] == JOB_STATUS_COMPLETED
    assert status_body["result"]["conversation_id"] == "conv-e2e"
