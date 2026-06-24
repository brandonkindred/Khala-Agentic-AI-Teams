"""Tests for Planning V3 job_store's user-profile association hook."""

from __future__ import annotations

from typing import Any, List

from planning_v3_team.shared import job_store
from user_profile import ArtifactType


class _CreateClient:
    def create_job(self, job_id, status=None, **data):
        pass


def _record_calls(monkeypatch) -> List[Any]:
    """Stub the job client + association recorder; return the captured calls."""
    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _CreateClient())
    calls: List[Any] = []
    monkeypatch.setattr(job_store, "record_association_safe", lambda *a, **k: calls.append((a, k)))
    return calls


def test_create_job_records_profile_association(monkeypatch):
    """create_job links the new project to the default user profile (best-effort)."""
    calls = _record_calls(monkeypatch)
    job_store.create_job("job_1", "my/repo")
    assert calls == [((ArtifactType.PROJECT, "planning_v3", "job_1"), {"label": "my/repo"})]


def test_create_job_label_falls_back_to_job_id(monkeypatch):
    """With no repo_path, the association label defaults to the job id."""
    calls = _record_calls(monkeypatch)
    job_store.create_job("job_2", None)
    job_store.create_job("job_3", "")
    assert calls == [
        ((ArtifactType.PROJECT, "planning_v3", "job_2"), {"label": "job_2"}),
        ((ArtifactType.PROJECT, "planning_v3", "job_3"), {"label": "job_3"}),
    ]
