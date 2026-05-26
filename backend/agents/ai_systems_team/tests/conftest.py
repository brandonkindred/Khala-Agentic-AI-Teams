import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _patched_job_store(monkeypatch, fake_job_client):
    """Route the ai_systems_team job_store's ``_client`` through the in-memory fake.

    Preconditions: pytest is running under ``backend/conftest.py`` so
        ``fake_job_client`` resolves to the function-scoped
        ``FakeJobServiceClient`` defined there.
    Postconditions: every call to ``ai_systems_team.shared.job_store._client(...)``
        within a test returns the same per-test ``FakeJobServiceClient`` instance;
        the real ``JobServiceClient`` is never constructed.
    """
    from ai_systems_team.shared import job_store as js

    monkeypatch.setattr(js, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client
