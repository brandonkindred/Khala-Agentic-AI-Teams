"""Test fixtures for the Startup Advisor team.

Routes the team's ``job_store`` through the shared in-memory fake so the unit
tests (dispatch branch, Temporal activity) exercise the FastAPI app and the
activity end-to-end without the real job service or Postgres. Integration-marked
tests keep using the real in-process job service, so the fake is not installed
for them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _patched_startup_advisor_job_client(request, monkeypatch, fake_job_client):
    """Route the team's job_store ``_client`` factory through the in-memory fake.

    A no-op for ``@pytest.mark.integration`` tests, which run against the real
    in-process job service. Clears the module-level singleton cache so a real
    client cached at import time can't leak in.
    """
    if request.node.get_closest_marker("integration"):
        return None

    from startup_advisor.shared import job_store as js

    monkeypatch.setattr(js, "_client_instance", None, raising=False)
    monkeypatch.setattr(js, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client
