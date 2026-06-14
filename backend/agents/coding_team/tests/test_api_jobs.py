"""Tests for GET /jobs: list serialization including the pause flag and GitHub context."""

from __future__ import annotations

from typing import Any, Dict

from fastapi.testclient import TestClient

from coding_team.api import main as api

client = TestClient(api.app)


def _job(**over: Any) -> Dict[str, Any]:
    base = {
        "job_id": "j1",
        "status": "running",
        "phase": "coding",
        "repo_path": "/tmp/repo",
    }
    base.update(over)
    return base


def test_jobs_serializes_github_context_and_pause_flag(monkeypatch):
    ctx = {
        "owner": "acme",
        "repo": "widgets",
        "issue_number": 42,
        "issue_url": "https://github.com/acme/widgets/issues/42",
    }
    monkeypatch.setattr(
        api,
        "list_jobs",
        lambda **kw: [
            _job(
                status="waiting_for_user",
                phase="paused",
                status_text="Paused for a decision",
                updated_at="2026-06-09T12:00:00Z",
                waiting_for_answers=True,
                github_context=ctx,
            )
        ],
    )
    r = client.get("/jobs")
    assert r.status_code == 200
    (item,) = r.json()
    assert item["job_id"] == "j1"
    assert item["status"] == "waiting_for_user"
    assert item["phase"] == "paused"
    assert item["status_text"] == "Paused for a decision"
    assert item["updated_at"] == "2026-06-09T12:00:00Z"
    assert item["waiting_for_answers"] is True
    assert item["github_context"] == ctx


def test_jobs_defaults_when_fields_absent(monkeypatch):
    monkeypatch.setattr(api, "list_jobs", lambda **kw: [_job()])
    r = client.get("/jobs")
    assert r.status_code == 200
    (item,) = r.json()
    assert item["status_text"] is None
    assert item["updated_at"] is None
    assert item["waiting_for_answers"] is False
    assert item["github_context"] is None


def test_jobs_coerces_truthy_pause_flag(monkeypatch):
    monkeypatch.setattr(api, "list_jobs", lambda **kw: [_job(waiting_for_answers=1)])
    (item,) = client.get("/jobs").json()
    assert item["waiting_for_answers"] is True


def test_jobs_empty_list(monkeypatch):
    monkeypatch.setattr(api, "list_jobs", lambda **kw: [])
    r = client.get("/jobs")
    assert r.status_code == 200
    assert r.json() == []


def test_jobs_active_filter_pushed_to_store(monkeypatch):
    """?active=true must filter at the job service (active_only) rather than client-side, so
    terminal jobs' full records never cross the wire."""
    seen = {}

    def fake_list_jobs(**kw):
        seen.update(kw)
        return []

    monkeypatch.setattr(api, "list_jobs", fake_list_jobs)
    assert client.get("/jobs?active=true").status_code == 200
    assert seen == {"active_only": True}
    assert client.get("/jobs").status_code == 200
    assert seen == {"active_only": False}
