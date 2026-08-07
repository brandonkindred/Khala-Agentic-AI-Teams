import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _patched_job_store(monkeypatch, fake_job_client):
    """Route the team's job_store ``_client`` through the in-memory fake."""
    from agent_team_studio.agent_provisioning_team.shared import job_store as js

    monkeypatch.setattr(js, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client
