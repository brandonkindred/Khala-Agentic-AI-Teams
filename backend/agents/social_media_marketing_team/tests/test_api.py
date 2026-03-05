import time
from pathlib import Path

from fastapi.testclient import TestClient

from social_media_marketing_team.api.main import app
from social_media_marketing_team.models import Platform


def _wait_for_done(client: TestClient, job_id: str):
    deadline = time.time() + 5
    status_payload = None
    while time.time() < deadline:
        status_resp = client.get(f"/social-marketing/status/{job_id}")
        assert status_resp.status_code == 200
        status_payload = status_resp.json()
        if status_payload["status"] in {"completed", "failed"}:
            break
        time.sleep(0.05)
    return status_payload


def test_run_endpoint_and_status_success(tmp_path: Path) -> None:
    client = TestClient(app)
    guidelines = tmp_path / "guidelines.md"
    objectives = tmp_path / "objectives.md"
    guidelines.write_text("Tone: expert but friendly")
    objectives.write_text("Goals: engagement and qualified leads")

    resp = client.post(
        "/social-marketing/run",
        json={
            "brand_guidelines_path": str(guidelines),
            "brand_objectives_path": str(objectives),
            "llm_model_name": "llama3.1",
            "brand_name": "Acme",
            "target_audience": "B2B founders",
            "human_approved_for_testing": True,
        },
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    status_payload = _wait_for_done(client, job_id)
    assert status_payload is not None
    assert status_payload["status"] == "completed"
    assert status_payload["progress"] == 100
    assert status_payload["llm_model_name"] == "llama3.1"
    assert status_payload["eta_hint"] == "done"
    assert status_payload["result"]["llm_model_name"] == "llama3.1"


def test_performance_ingest_and_revision_endpoints(tmp_path: Path) -> None:
    client = TestClient(app)
    guidelines = tmp_path / "guidelines.md"
    objectives = tmp_path / "objectives.md"
    guidelines.write_text("Tone: expert")
    objectives.write_text("Goals: engagement")

    run = client.post(
        "/social-marketing/run",
        json={
            "brand_guidelines_path": str(guidelines),
            "brand_objectives_path": str(objectives),
            "llm_model_name": "mistral",
            "human_approved_for_testing": True,
            "brand_name": "Acme",
        },
    )
    assert run.status_code == 200
    job_id = run.json()["job_id"]

    ingest = client.post(
        f"/social-marketing/performance/{job_id}",
        json={
            "observations": [
                {
                    "campaign_name": "Acme multi-platform growth sprint",
                    "platform": Platform.LINKEDIN.value,
                    "concept_title": "Practical education spotlight #1",
                    "posted_at": "2026-01-01T00:00:00Z",
                    "metrics": [{"name": "engagement_rate", "value": 0.85}],
                }
            ]
        },
    )
    assert ingest.status_code == 200
    assert ingest.json()["observations_ingested"] == 1

    revise = client.post(
        f"/social-marketing/revise/{job_id}",
        json={"feedback": "Please emphasize follower growth.", "approved_for_testing": True},
    )
    assert revise.status_code == 200
    assert revise.json()["status"] == "running"

    status = _wait_for_done(client, job_id)
    assert status is not None
    assert status["status"] == "completed"


def test_run_endpoint_rejects_missing_files() -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/run",
        json={
            "brand_guidelines_path": "/tmp/does-not-exist-1.md",
            "brand_objectives_path": "/tmp/does-not-exist-2.md",
            "llm_model_name": "llama3.1",
        },
    )
    assert resp.status_code == 400


def test_status_and_performance_404_for_unknown_job() -> None:
    client = TestClient(app)
    resp = client.get("/social-marketing/status/not-a-job")
    assert resp.status_code == 404

    perf = client.post("/social-marketing/performance/not-a-job", json={"observations": []})
    assert perf.status_code == 404
