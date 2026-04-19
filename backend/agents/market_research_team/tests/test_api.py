"""API tests for the Market Research team — async-only job flow."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

from market_research_team.api import main as api_main
from market_research_team.api.main import app

client = TestClient(app)


def _poll(client: TestClient, job_id: str, deadline_s: float = 5.0) -> Dict[str, Any]:
    start = time.time()
    while time.time() - start < deadline_s:
        r = client.get(f"/market-research/status/{job_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if data.get("status") in {"completed", "failed", "cancelled"}:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Job {job_id} did not terminate in {deadline_s}s")


def test_market_research_run_endpoint_needs_human_decision() -> None:
    response = client.post(
        "/market-research/run",
        json={
            "product_concept": "Interview analysis assistant",
            "target_users": "startup founders",
            "business_goal": "validate demand faster",
            "topology": "split",
            "transcripts": ["Users want confidence before building features."],
            "human_approved": False,
        },
    )

    assert response.status_code == 200
    submission = response.json()
    assert "job_id" in submission
    assert submission["status"] in {"pending", "running"}

    final = _poll(client, submission["job_id"])
    assert final["status"] == "completed"
    result = final["result"]
    assert result["topology"] == "split"
    assert result["status"] == "needs_human_decision"
    assert isinstance(result["proposed_research_scripts"], list)


def test_market_research_run_endpoint_ready_for_execution_with_folder(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "one.txt").write_text(
        "I am trying to automate onboarding.\nPain is manual steps.", encoding="utf-8"
    )

    response = client.post(
        "/market-research/run",
        json={
            "product_concept": "Onboarding copilot",
            "target_users": "CS teams",
            "business_goal": "cut time to first value",
            "topology": "unified",
            "transcript_folder_path": str(folder),
            "human_approved": True,
        },
    )

    assert response.status_code == 200
    submission = response.json()
    final = _poll(client, submission["job_id"])
    assert final["status"] == "completed"
    result = final["result"]
    assert result["status"] == "ready_for_execution"
    assert result["insights"]


def test_market_research_job_failure_captured(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BoomOrchestrator:
        def run(self, *_args, **_kwargs):  # pragma: no cover - used via monkeypatch
            raise RuntimeError("orchestrator exploded")

    monkeypatch.setattr(api_main, "MarketResearchOrchestrator", _BoomOrchestrator)

    response = client.post(
        "/market-research/run",
        json={
            "product_concept": "Broken concept",
            "target_users": "nobody",
            "business_goal": "fail fast",
            "topology": "unified",
            "human_approved": True,
        },
    )
    assert response.status_code == 200
    final = _poll(client, response.json()["job_id"])
    assert final["status"] == "failed"
    assert "orchestrator exploded" in (final.get("error") or "")


def test_status_404_for_unknown_job() -> None:
    r = client.get("/market-research/status/does-not-exist")
    assert r.status_code == 404


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
