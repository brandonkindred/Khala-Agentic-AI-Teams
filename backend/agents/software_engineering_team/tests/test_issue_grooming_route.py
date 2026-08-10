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
        json={"owner": "acme", "repo": "widgets", "issue_number": 42},
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
    # Tagged with job_type + github_context before dispatch.
    tag_job_id, tag_fields = updates[0]
    assert tag_job_id == body["job_id"]
    assert tag_fields["job_type"] == "issue_grooming"
    assert tag_fields["github_context"] == {"owner": "acme", "repo": "widgets", "issue_number": 42}


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
        json={"owner": "acme", "repo": "widgets", "issue_number": 42},
    )

    assert r.status_code == 503
    assert len(created) == 1
    # First update tags job_type/github_context, second marks it failed.
    assert len(updates) == 2
    job_id, kw = updates[-1]
    assert kw["status"] == "failed"
    assert "Temporal dispatch failed" in kw["error"]
    assert "worker unreachable" in kw["error"]
