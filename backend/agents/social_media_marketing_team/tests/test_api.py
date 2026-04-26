import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from social_media_marketing_team.adapters.branding import (
    BrandContext,
    BrandNotFoundError,
)
from social_media_marketing_team.api.main import app
from social_media_marketing_team.models import Platform

# Hits the team API which calls the real job service.  See
# ``test_api_internal.py`` for the converted unit-style equivalent.
pytestmark = [pytest.mark.integration]

_MOCK_BRAND_CTX = BrandContext(
    brand_name="Acme",
    target_audience="B2B founders and engineering leaders",
    voice_and_tone="clear, confident, human",
    brand_guidelines="Positioning: Developer tools that just work.",
    brand_objectives="Purpose: Empower developers.\nMission: Ship faster.",
    messaging_pillars=["Developer empowerment", "Simplicity"],
    brand_story="Acme was born from frustration with overcomplicated tools.",
    tagline="Developer tools that just work",
)

_BRAND_ADAPTER = "social_media_marketing_team.api.main"


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


@patch(f"{_BRAND_ADAPTER}._fetch_and_validate_brand", return_value=_MOCK_BRAND_CTX)
def test_run_endpoint_and_status_success(_mock_brand) -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_1",
            "brand_id": "brand_1",
            "llm_model_name": "llama3.1",
            "human_approved_for_testing": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    job_id = body["job_id"]
    assert body["brand_summary"] is not None
    assert "Acme" in body["brand_summary"]

    status_payload = _wait_for_done(client, job_id)
    assert status_payload is not None
    assert status_payload["status"] == "completed"
    assert status_payload["progress"] == 100
    assert status_payload["llm_model_name"] == "llama3.1"
    assert status_payload["eta_hint"] == "done"
    assert status_payload["result"]["llm_model_name"] == "llama3.1"


@patch(f"{_BRAND_ADAPTER}._fetch_and_validate_brand", return_value=_MOCK_BRAND_CTX)
def test_performance_ingest_and_revision_endpoints(_mock_brand) -> None:
    client = TestClient(app)
    run = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_1",
            "brand_id": "brand_1",
            "llm_model_name": "mistral",
            "human_approved_for_testing": True,
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


@patch(
    f"{_BRAND_ADAPTER}.fetch_brand",
    side_effect=BrandNotFoundError("client_x", "brand_x"),
)
def test_run_endpoint_rejects_missing_brand(_mock_fetch) -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_x",
            "brand_id": "brand_x",
            "llm_model_name": "llama3.1",
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "brand_not_found"
    assert "brand_x" in detail["message"]
    assert "user_message" in detail


@patch(
    f"{_BRAND_ADAPTER}.fetch_brand",
    return_value={
        "latest_output": {"strategic_core": {"some": "data"}},
        "current_phase": "strategic_core",
    },
)
def test_run_endpoint_rejects_incomplete_brand(_mock_fetch) -> None:
    client = TestClient(app)
    resp = client.post(
        "/social-marketing/run",
        json={
            "client_id": "client_y",
            "brand_id": "brand_y",
            "llm_model_name": "llama3.1",
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "brand_incomplete"
    assert "narrative_messaging" in detail["missing_phases"]
    assert "user_message" in detail


def test_status_and_performance_404_for_unknown_job() -> None:
    client = TestClient(app)
    resp = client.get("/social-marketing/status/not-a-job")
    assert resp.status_code == 404

    perf = client.post("/social-marketing/performance/not-a-job", json={"observations": []})
    assert perf.status_code == 404
