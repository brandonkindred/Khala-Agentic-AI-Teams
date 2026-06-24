"""Tests for job_store's user-profile association hook."""

from __future__ import annotations

from typing import Any, List

from coding_team import job_store
from user_profile import ArtifactType


def test_create_job_records_profile_association(monkeypatch):
    """create_job links the new project to the default user profile (best-effort)."""

    class _CreateClient:
        def create_job(self, job_id, status="pending", **data):
            pass

    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _CreateClient())
    calls: List[Any] = []
    monkeypatch.setattr(job_store, "record_association_safe", lambda *a, **k: calls.append((a, k)))

    job_store.create_job("job_1", "my/repo")
    assert calls == [((ArtifactType.PROJECT, "coding_team", "job_1"), {"label": "my/repo"})]


def test_create_job_label_falls_back_to_job_id(monkeypatch):
    """With no repo_path, the association label defaults to the job id."""

    class _CreateClient:
        def create_job(self, job_id, status="pending", **data):
            pass

    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _CreateClient())
    calls: List[Any] = []
    monkeypatch.setattr(job_store, "record_association_safe", lambda *a, **k: calls.append((a, k)))

    job_store.create_job("job_2", "")
    assert calls == [((ArtifactType.PROJECT, "coding_team", "job_2"), {"label": "job_2"})]
