"""API tests for Medium stats endpoints (agent mocked — no browser).

Backed by an in-memory FakeJobServiceClient — no Postgres or live job service
required.  The async tests still poll a real background thread for completion.
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_blogging_root = Path(__file__).resolve().parent.parent
if str(_blogging_root) not in sys.path:
    sys.path.insert(0, str(_blogging_root))

_spec = importlib.util.spec_from_file_location(
    "blogging_api_main_medium",
    _blogging_root / "api" / "main.py",
)
_api_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_api_main)
app = _api_main.app


@pytest.fixture(autouse=True)
def _patched_blog_client(monkeypatch, fake_job_client):
    from shared import blog_job_store as bjs

    monkeypatch.setattr(bjs, "_client", lambda *a, **kw: fake_job_client)
    return fake_job_client


@pytest.fixture(autouse=True)
def _medium_stats_tmp_dir(monkeypatch, tmp_path):
    from shared import blog_job_store as bjs

    monkeypatch.setattr(
        _api_main,
        "medium_stats_run_dir",
        lambda jid, **kw: bjs.medium_stats_run_dir(jid, cache_dir=tmp_path),
    )


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_medium_stats_sync_returns_503_without_integration(client: TestClient) -> None:
    """Medium stats requires the Medium.com integration to be enabled and configured."""
    r = client.post("/medium-stats", json={"headless": True})
    assert r.status_code == 503
    detail = str(r.json().get("detail", "")).lower()
    assert "medium" in detail or "integration" in detail


def test_medium_stats_async_writes_artifact(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async job completes and persists medium_stats_report.json when collect is mocked."""
    from blog_medium_stats_agent.models import MediumPostStats, MediumStatsReport

    class FakeBlogMediumStatsAgent:
        def collect(self, cfg=None):
            return MediumStatsReport(
                account_hint="example.com",
                posts=[
                    MediumPostStats(
                        title="Test Post",
                        url="https://medium.com/@x/test-post",
                        stats={"views": 100},
                    ),
                ],
            )

    monkeypatch.setattr(_api_main, "BlogMediumStatsAgent", FakeBlogMediumStatsAgent)
    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    r = client.post("/medium-stats-async", json={"headless": True})
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deadline = time.time() + 5.0
    status = "pending"
    st_json = {}
    while time.time() < deadline:
        st = client.get(f"/job/{job_id}")
        if st.status_code == 200:
            st_json = st.json()
            status = st_json.get("status", "")
            if status == "completed":
                break
        time.sleep(0.05)

    assert status == "completed", st_json

    art = client.get(f"/job/{job_id}/artifacts")
    assert art.status_code == 200
    names = {a["name"] for a in art.json()["artifacts"]}
    assert "medium_stats_report.json" in names

    content = client.get(f"/job/{job_id}/artifacts/medium_stats_report.json")
    assert content.status_code == 200
    body = content.json()
    assert body["content"]["posts"][0]["title"] == "Test Post"


def test_jobs_list_includes_job_type_for_medium_stats(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from blog_medium_stats_agent.models import MediumStatsReport

    class FakeBlogMediumStatsAgent:
        def collect(self, cfg=None):
            return MediumStatsReport(posts=[])

    monkeypatch.setattr(_api_main, "BlogMediumStatsAgent", FakeBlogMediumStatsAgent)
    monkeypatch.setattr(_api_main, "medium_stats_integration_eligible", lambda: (True, ""))

    r = client.post("/medium-stats-async", json={})
    job_id = r.json()["job_id"]
    deadline = time.time() + 5.0
    while time.time() < deadline:
        st = client.get(f"/job/{job_id}")
        if st.status_code == 200 and st.json().get("status") == "completed":
            break
        time.sleep(0.05)

    listed = client.get("/jobs")
    assert listed.status_code == 200
    row = next((j for j in listed.json() if j["job_id"] == job_id), None)
    assert row is not None
    assert row.get("job_type") == "medium_stats"
