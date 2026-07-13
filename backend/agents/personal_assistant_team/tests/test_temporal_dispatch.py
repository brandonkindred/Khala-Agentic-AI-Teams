"""Tests for the ``POST /assistant/jobs`` Temporal-vs-thread dispatch branch.

Covers the Temporal branch (patched enabled) and the thread fallback without a
running Temporal server or job service (the job store is faked).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from personal_assistant_team.api import main as api_main
from personal_assistant_team.api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fake_job_store(monkeypatch):
    from job_service_client_fake import FakeJobServiceClient

    fake = FakeJobServiceClient(team="personal_assistant_team")
    monkeypatch.setattr("personal_assistant_team.shared.pa_job_store._client", lambda *a, **k: fake)
    return fake


def test_dispatches_to_temporal_when_enabled(monkeypatch):
    monkeypatch.setattr("personal_assistant_team.temporal.client.is_temporal_enabled", lambda: True)
    captured: dict = {}
    monkeypatch.setattr(
        "personal_assistant_team.temporal.start_workflow.start_assistant_workflow",
        lambda job_id, user_id, message, context: captured.update(
            job_id=job_id, user_id=user_id, message=message, context=context
        ),
    )

    def _no_thread(*_a, **_k):  # pragma: no cover - asserts the thread path is skipped
        raise AssertionError("thread path must not run when Temporal is enabled")

    monkeypatch.setattr(api_main.threading, "Thread", _no_thread)

    response = client.post(
        "/assistant/jobs", params={"user_id": "u1"}, json={"message": "hi there"}
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert captured["job_id"] == job_id
    assert captured["user_id"] == "u1"
    assert captured["message"] == "hi there"


def test_falls_back_to_thread_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "personal_assistant_team.temporal.client.is_temporal_enabled", lambda: False
    )

    started: dict = {}

    class _FakeThread:
        def __init__(self, *, target, args, daemon):
            started["target"] = target
            started["args"] = args
            started["daemon"] = daemon

        def start(self):
            started["started"] = True

    monkeypatch.setattr(api_main.threading, "Thread", _FakeThread)

    response = client.post(
        "/assistant/jobs", params={"user_id": "u2"}, json={"message": "read inbox"}
    )

    assert response.status_code == 200
    assert started["started"] is True
    assert started["daemon"] is True
    assert started["target"] is api_main._run_assistant_job
    job_id = response.json()["job_id"]
    assert started["args"][0] == job_id
    assert started["args"][1] == "u2"
    assert started["args"][2] == "read inbox"
