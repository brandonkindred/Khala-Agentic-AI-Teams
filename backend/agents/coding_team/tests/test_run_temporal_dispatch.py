"""POST /run branches to Temporal when enabled, else the daemon-thread path.

Both paths return the same job_id and create exactly one job row; only the
execution surface differs.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import shared_temporal
from coding_team.api import main as api
from coding_team.temporal import start_workflow as sw

client = TestClient(api.app)


def test_run_dispatches_via_temporal_when_enabled(monkeypatch):
    created: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)

    dispatched: dict = {}
    monkeypatch.setattr(
        sw,
        "start_coding_team_workflow",
        lambda job_id, repo_path, plan_input: dispatched.update(
            job_id=job_id, repo_path=repo_path, plan_input=plan_input
        ),
    )
    # The thread path must NOT run when Temporal handles the dispatch.
    monkeypatch.setattr(
        api,
        "run_orchestrator_wired",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("thread path ran under Temporal")),
    )

    r = client.post("/run", json={"repo_path": "/repo", "plan_input": {"objective": "x"}})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert dispatched["job_id"] == body["job_id"]  # workflow reuses the API's job_id
    assert dispatched["repo_path"] == "/repo"
    assert dispatched["plan_input"] == {"objective": "x"}
    assert len(created) == 1  # exactly one row, created by the API


def test_run_uses_thread_path_when_temporal_disabled(monkeypatch):
    created: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: False)

    def _fail_dispatch(*a, **k):
        raise AssertionError("Temporal dispatch ran while disabled")

    monkeypatch.setattr(sw, "start_coding_team_workflow", _fail_dispatch)
    # Neutralize the daemon-thread body so the test doesn't run a real orchestrator.
    monkeypatch.setattr(api, "plan_from_input", lambda plan, repo: plan)
    monkeypatch.setattr(api, "run_orchestrator_wired", lambda *a, **k: None)
    monkeypatch.setattr(api, "_register_run_thread", lambda job_id: None)
    monkeypatch.setattr(api, "_clear_run_thread", lambda job_id: None)

    r = client.post("/run", json={"repo_path": "/repo", "plan_input": {"objective": "x"}})

    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert len(created) == 1


def test_run_marks_job_failed_and_503_when_temporal_dispatch_raises(monkeypatch):
    """When Temporal is enabled but the worker isn't reachable, the dispatch
    raises. The route must mark the freshly-created row failed (not leave it
    orphaned in 'pending') and return a retryable 503 — and must NOT fall back
    to the thread path (avoids double-running an already-scheduled workflow)."""
    created: list = []
    updates: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(api, "update_job", lambda job_id, **kw: updates.append((job_id, kw)))
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)

    def _raise(*a, **k):
        raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr(sw, "start_coding_team_workflow", _raise)
    # The thread path must NOT run as a fallback.
    monkeypatch.setattr(
        api,
        "run_orchestrator_wired",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("fell back to thread path")),
    )

    r = client.post("/run", json={"repo_path": "/repo", "plan_input": {"objective": "x"}})

    assert r.status_code == 503
    assert len(created) == 1
    assert len(updates) == 1
    job_id, kw = updates[0]
    assert kw["status"] == "failed"
    assert "Temporal dispatch failed" in kw["error"]


def test_run_without_plan_input_creates_row_and_stays_pending(monkeypatch):
    """A job-only request (no plan) never dispatches to either surface."""
    created: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(shared_temporal, "is_temporal_enabled", lambda: True)
    monkeypatch.setattr(
        sw,
        "start_coding_team_workflow",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched with no plan")),
    )

    r = client.post("/run", json={"repo_path": "/repo"})

    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert len(created) == 1
