"""Tests for Planning V3 job_store — profile-association hook."""

import sys
from pathlib import Path

_agents_dir = Path(__file__).resolve().parent.parent.parent
if str(_agents_dir) not in sys.path:
    sys.path.insert(0, str(_agents_dir))

from planning_v3_team.shared import job_store  # noqa: E402
from user_profile import ArtifactType  # noqa: E402


def test_create_job_records_profile_association(monkeypatch):
    """create_job links the new project to the default user profile (best-effort)."""

    class _CreateClient:
        def create_job(self, job_id, status=None, **data):
            pass

    monkeypatch.setattr(job_store, "_client", lambda cache_dir=None: _CreateClient())
    calls: list = []
    monkeypatch.setattr(job_store, "record_association_async", lambda *a, **k: calls.append((a, k)))

    job_store.create_job("job_1", "my/repo")
    assert calls == [((ArtifactType.PROJECT, "planning_v3", "job_1"), {"label": "my/repo"})]
