"""Tests for the ``POST /conversation/messages`` Temporal-vs-thread dispatch branch.

With ``TEMPORAL_ADDRESS`` unset ``is_temporal_enabled()`` is False, so the
thread path already runs unchanged in normal operation. These tests cover the
Temporal branch (patched enabled) and the dispatch-failure handling, without
needing a running Temporal server.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from startup_advisor.api import main as api_main
from startup_advisor.shared.job_store import JOB_STATUS_FAILED


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
