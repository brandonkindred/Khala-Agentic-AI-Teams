from pathlib import Path

from fastapi.testclient import TestClient

from market_research_team.api.main import app


client = TestClient(app)


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
    data = response.json()
    assert data["topology"] == "split"
    assert data["status"] == "needs_human_decision"
    assert isinstance(data["proposed_research_scripts"], list)


def test_market_research_run_endpoint_ready_for_execution_with_folder(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    (folder / "one.txt").write_text("I am trying to automate onboarding.\nPain is manual steps.", encoding="utf-8")

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
    data = response.json()
    assert data["status"] == "ready_for_execution"
    assert data["insights"]


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
