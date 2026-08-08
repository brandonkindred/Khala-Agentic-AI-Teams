"""POST /run dispatches to Temporal unconditionally — no thread fallback.

Creates exactly one job row and returns its job_id; a plan-less request
never dispatches at all.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from software_engineering_team.api import coding_team_main as api
from software_engineering_team.temporal import coding_team_start_workflow as sw

client = TestClient(api.app)


def test_run_dispatches_via_temporal_when_enabled(monkeypatch):
    created: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)

    dispatched: dict = {}
    monkeypatch.setattr(
        sw,
        "start_coding_team_workflow",
        lambda job_id, repo_path, plan_input: dispatched.update(
            job_id=job_id, repo_path=repo_path, plan_input=plan_input
        ),
    )

    r = client.post("/run", json={"repo_path": "/repo", "plan_input": {"objective": "x"}})

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert dispatched["job_id"] == body["job_id"]  # workflow reuses the API's job_id
    assert dispatched["repo_path"] == "/repo"
    assert dispatched["plan_input"] == {"objective": "x"}
    assert len(created) == 1  # exactly one row, created by the API


def test_run_marks_job_failed_and_503_when_temporal_dispatch_raises(monkeypatch):
    """When the worker isn't reachable, dispatch raises. The route must mark the
    freshly-created row failed (not leave it orphaned in 'pending') and return a
    retryable 503."""
    created: list = []
    updates: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(api, "update_job", lambda job_id, **kw: updates.append((job_id, kw)))

    def _raise(*a, **k):
        raise RuntimeError("Temporal client not available; is the team's worker running?")

    monkeypatch.setattr(sw, "start_coding_team_workflow", _raise)

    r = client.post("/run", json={"repo_path": "/repo", "plan_input": {"objective": "x"}})

    assert r.status_code == 503
    assert len(created) == 1
    assert len(updates) == 1
    job_id, kw = updates[0]
    assert kw["status"] == "failed"
    assert "Temporal dispatch failed" in kw["error"]


def test_run_without_plan_input_creates_row_and_stays_pending(monkeypatch):
    """A job-only request (no plan) never dispatches."""
    created: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(
        sw,
        "start_coding_team_workflow",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dispatched with no plan")),
    )

    r = client.post("/run", json={"repo_path": "/repo"})

    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert len(created) == 1
