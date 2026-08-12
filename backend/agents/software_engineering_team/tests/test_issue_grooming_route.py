"""POST /groom-github-issues dispatches to Temporal unconditionally.

Creates exactly one job row, tags it with the GitHub issue context, and
starts IssueGroomingWorkflow. A dispatch failure marks the freshly-created
row failed (no orphaned pending row) and surfaces a retryable 503 -- same
shape as POST /run and POST /run-from-github.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from software_engineering_team.api import coding_team_main as api
from software_engineering_team.api.routes import issue_grooming as route_mod

client = TestClient(api.app)


def test_groom_dispatches_via_temporal(monkeypatch):
    created: list = []
    updates: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(api, "update_job", lambda job_id, **kw: updates.append((job_id, kw)))

    dispatched: dict = {}
    monkeypatch.setattr(
        route_mod,
        "start_issue_grooming_workflow",
        lambda job_id, owner, repo, issue_number: dispatched.update(
            job_id=job_id, owner=owner, repo=repo, issue_number=issue_number
        ),
    )

    r = client.post(
        "/groom-github-issues",
        json={
            "owner": "acme",
            "repo": "widgets",
            "issue_number": 42,
            "github_token": "fake-token",
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["issue_number"] == 42
    assert dispatched["job_id"] == body["job_id"]
    assert dispatched["owner"] == "acme"
    assert dispatched["repo"] == "widgets"
    assert dispatched["issue_number"] == 42
    assert len(created) == 1
    assert created[0]["repo_path"] == "acme/widgets"
    # Tagged with job_type + github_context (and, when an encryption key is
    # configured, github_token_encrypted -- never the plaintext token) before
    # dispatch.
    tag_job_id, tag_fields = updates[0]
    assert tag_job_id == body["job_id"]
    assert tag_fields["job_type"] == "issue_grooming"
    assert tag_fields["github_context"] == {"owner": "acme", "repo": "widgets", "issue_number": 42}
    assert "github_token" not in tag_fields


def test_groom_requires_a_github_token(monkeypatch):
    """Neither a request-body token nor GITHUB_TOKEN env -> 400, no job created."""
    created: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    r = client.post(
        "/groom-github-issues",
        json={"owner": "acme", "repo": "widgets", "issue_number": 42},
    )

    assert r.status_code == 400
    assert created == []


def test_groom_persists_encrypted_token_on_the_job(monkeypatch):
    """The resolved token is encrypted and stored as github_token_encrypted --
    the same field run_issue_grooming_activity resolves from -- never the
    plaintext token."""
    updates: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: None, raising=True)
    monkeypatch.setattr(api, "update_job", lambda job_id, **kw: updates.append((job_id, kw)))
    monkeypatch.setattr(route_mod, "start_issue_grooming_workflow", lambda *a, **k: None)
    monkeypatch.setattr(route_mod, "encrypt_token", lambda token: f"encrypted::{token}")

    r = client.post(
        "/groom-github-issues",
        json={
            "owner": "acme",
            "repo": "widgets",
            "issue_number": 42,
            "github_token": "super-secret-pat",
        },
    )

    assert r.status_code == 200
    _tag_job_id, tag_fields = updates[0]
    assert tag_fields["github_token_encrypted"] == "encrypted::super-secret-pat"


def test_groom_marks_job_failed_and_503_when_temporal_dispatch_raises(monkeypatch):
    """When the worker isn't reachable, dispatch raises. The route must mark the
    freshly-created row failed (not leave it orphaned in 'pending') and return a
    retryable 503."""
    created: list = []
    updates: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr(api, "update_job", lambda job_id, **kw: updates.append((job_id, kw)))

    def _raise(*a, **k):
        raise RuntimeError("worker unreachable")

    monkeypatch.setattr(route_mod, "start_issue_grooming_workflow", _raise)

    r = client.post(
        "/groom-github-issues",
        json={
            "owner": "acme",
            "repo": "widgets",
            "issue_number": 42,
            "github_token": "fake-token",
        },
    )

    assert r.status_code == 503
    assert len(created) == 1
    # First update tags job_type/github_context, second marks it failed.
    assert len(updates) == 2
    job_id, kw = updates[-1]
    assert kw["status"] == "failed"
    assert "Temporal dispatch failed" in kw["error"]
    assert "worker unreachable" in kw["error"]


# --------------------------------------------------------------------------- /status grooming parity


def _job(**over):
    base = {
        "job_id": "j1",
        "status": "running",
        "phase": "phase_a",
        "task_graph_snapshot": [],
    }
    base.update(over)
    return base


def test_status_surfaces_in_flight_grooming_score(monkeypatch):
    """Phase A completes before Phase B runs (or is skipped): grooming carries only
    'score', same dict IssueGroomingRunner passes to update_job(grooming=...)."""
    grooming = {"score": {"conceptual": 2, "loc": 2, "solution": 2, "aggregate": 2}}
    monkeypatch.setattr(api, "get_job", lambda jid: _job(grooming=grooming))
    r = client.get("/status/j1")
    assert r.status_code == 200
    assert r.json()["grooming"] == grooming


def test_status_surfaces_terminal_grooming_sub_issues(monkeypatch):
    """A successful run that split the issue: grooming adds 'sub_issues' and the job
    is terminal -- both surface unchanged through the status endpoint."""
    grooming = {
        "score": {"conceptual": 5, "loc": 5, "solution": 8, "aggregate": 8},
        "sub_issues": [{"number": 101, "title": "Part 1"}, {"number": 102, "title": "Part 2"}],
    }
    monkeypatch.setattr(
        api, "get_job", lambda jid: _job(status="completed", phase="done", grooming=grooming)
    )
    r = client.get("/status/j1")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["grooming"] == grooming


def test_status_grooming_absent_is_null(monkeypatch):
    """A non-grooming job (or a grooming job before Phase A writes anything) has no
    'grooming' key -- the field defaults to null, not an error or empty dict."""
    monkeypatch.setattr(api, "get_job", lambda jid: _job())
    r = client.get("/status/j1")
    assert r.status_code == 200
    assert r.json()["grooming"] is None
