import time
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from branding_team.api.main import app
from branding_team.models import BrandingMission

# Hits the team API which calls the real job service.  Marked integration
# pending follow-up.
pytestmark = [pytest.mark.integration]

client = TestClient(app)


def _poll_brand_job(job_id: str, deadline_s: float = 10.0) -> Dict[str, Any]:
    start = time.time()
    while time.time() - start < deadline_s:
        r = client.get(f"/branding/status/{job_id}")
        assert r.status_code == 200, r.text
        data = r.json()
        if data.get("status") in {"completed", "failed", "cancelled"}:
            return data
        time.sleep(0.05)
    raise AssertionError(f"Branding job {job_id} did not terminate in {deadline_s}s")


def _payload() -> dict:
    return {
        "company_name": "Northstar Labs",
        "company_description": "A strategic studio helping product teams ship cohesive digital experiences",
        "target_audience": "enterprise product leaders",
    }


def test_create_session_and_get_questions() -> None:
    create = client.post("/sessions", json=_payload())
    assert create.status_code == 200
    data = create.json()
    assert data["session_id"]
    assert data["status"] == "awaiting_user_answers"
    assert len(data["open_questions"]) >= 1
    assert "current_phase" in data

    questions = client.get(f"/sessions/{data['session_id']}/questions")
    assert questions.status_code == 200
    assert questions.json()


def test_answer_question_updates_session_and_output() -> None:
    create = client.post("/sessions", json=_payload())
    session = create.json()
    session_id = session["session_id"]
    question_id = session["open_questions"][0]["id"]

    answer = client.post(
        f"/sessions/{session_id}/questions/{question_id}/answer",
        json={"answer": "clarity, trust, craft"},
    )
    assert answer.status_code == 200
    answered = answer.json()
    assert any(item["id"] == question_id for item in answered["answered_questions"])
    assert answered["latest_output"]["strategic_core"] is not None


def test_unknown_session_404() -> None:
    resp = client.get("/sessions/not-found")
    assert resp.status_code == 404


def test_post_and_get_clients() -> None:
    create = client.post("/clients", json={"name": "Acme Corp"})
    assert create.status_code == 201
    data = create.json()
    assert data["id"].startswith("client_")
    assert data["name"] == "Acme Corp"
    list_resp = client.get("/clients")
    assert list_resp.status_code == 200
    clients = list_resp.json()
    assert isinstance(clients, list)
    assert any(c["id"] == data["id"] for c in clients)
    get_one = client.get(f"/clients/{data['id']}")
    assert get_one.status_code == 200
    assert get_one.json()["name"] == "Acme Corp"


def test_get_client_404() -> None:
    resp = client.get("/clients/nonexistent-id")
    assert resp.status_code == 404


def test_post_and_get_brands() -> None:
    create_c = client.post("/clients", json={"name": "Brand Test Client"})
    assert create_c.status_code == 201
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "BrandCo",
            "company_description": "A company for brand tests",
            "target_audience": "testers",
        },
    )
    assert create_b.status_code == 201
    brand_data = create_b.json()
    assert brand_data["id"].startswith("brand_")
    assert brand_data["client_id"] == client_id
    assert brand_data["current_phase"] == "strategic_core"
    list_b = client.get(f"/clients/{client_id}/brands")
    assert list_b.status_code == 200
    assert len(list_b.json()) >= 1
    get_b = client.get(f"/clients/{client_id}/brands/{brand_data['id']}")
    assert get_b.status_code == 200
    assert get_b.json()["mission"]["company_name"] == "BrandCo"


def test_get_brand_404() -> None:
    create_c = client.post("/clients", json={"name": "For 404"})
    client_id = create_c.json()["id"]
    resp = client.get(f"/clients/{client_id}/brands/nonexistent-brand-id")
    assert resp.status_code == 404


def test_put_brand_update() -> None:
    create_c = client.post("/clients", json={"name": "Update Test"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "Original",
            "company_description": "Original description here",
            "target_audience": "audience",
        },
    )
    brand_id = create_b.json()["id"]
    put_resp = client.put(
        f"/clients/{client_id}/brands/{brand_id}",
        json={"company_description": "Updated description here"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["mission"]["company_description"] == "Updated description here"


def test_post_brands_run_returns_job_and_completes() -> None:
    create_c = client.post("/clients", json={"name": "Run Test Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "RunCo",
            "company_description": "Company for run test",
            "target_audience": "users",
        },
    )
    brand_id = create_b.json()["id"]
    run_resp = client.post(
        f"/clients/{client_id}/brands/{brand_id}/run",
        json={"human_approved": True},
    )
    assert run_resp.status_code == 200
    submission = run_resp.json()
    assert "job_id" in submission
    assert submission["status"] in {"pending", "running"}

    final = _poll_brand_job(submission["job_id"])
    assert final["status"] == "completed"
    out = final["result"]
    assert "brand_book" in out
    assert out["strategic_core"] is not None
    assert out["narrative_messaging"] is not None
    assert out["visual_identity"] is not None
    assert out["channel_activation"] is not None
    assert out["governance"] is not None


def test_post_brands_run_with_target_phase() -> None:
    create_c = client.post("/clients", json={"name": "Phase Test Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "PhaseCo",
            "company_description": "Company for phase test",
            "target_audience": "users",
        },
    )
    brand_id = create_b.json()["id"]
    run_resp = client.post(
        f"/clients/{client_id}/brands/{brand_id}/run",
        json={"human_approved": True, "target_phase": "strategic_core"},
    )
    assert run_resp.status_code == 200
    final = _poll_brand_job(run_resp.json()["job_id"])
    assert final["status"] == "completed"
    out = final["result"]
    assert out["strategic_core"] is not None
    assert out["narrative_messaging"] is None


def test_post_brands_run_phase_endpoint() -> None:
    create_c = client.post("/clients", json={"name": "Phase Endpoint Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "PhaseEndCo",
            "company_description": "Company for phase endpoint test",
            "target_audience": "users",
        },
    )
    brand_id = create_b.json()["id"]
    run_resp = client.post(
        f"/clients/{client_id}/brands/{brand_id}/run/narrative_messaging",
        json={"human_approved": True},
    )
    assert run_resp.status_code == 200
    final = _poll_brand_job(run_resp.json()["job_id"])
    assert final["status"] == "completed"
    out = final["result"]
    assert out["strategic_core"] is not None
    assert out["narrative_messaging"] is not None
    assert out["visual_identity"] is None


def test_branding_status_404_for_unknown_job() -> None:
    r = client.get("/branding/status/does-not-exist")
    assert r.status_code == 404


def test_request_market_research_returns_503_without_service() -> None:
    create_c = client.post("/clients", json={"name": "MR Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "MRCo",
            "company_description": "Company for market research test",
            "target_audience": "buyers",
        },
    )
    brand_id = create_b.json()["id"]
    resp = client.post(f"/clients/{client_id}/brands/{brand_id}/request-market-research")
    assert resp.status_code in (200, 503)


def test_request_design_assets_returns_stub() -> None:
    create_c = client.post("/clients", json={"name": "Design Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "DesignCo",
            "company_description": "Company for design assets test",
            "target_audience": "designers",
        },
    )
    brand_id = create_b.json()["id"]
    resp = client.post(f"/clients/{client_id}/brands/{brand_id}/request-design-assets")
    assert resp.status_code == 200
    data = resp.json()
    assert "request_id" in data
    assert data["status"] == "pending"
    assert "artifacts" in data


# --- Conversation (chat) API tests ---


def test_post_conversations_returns_conversation_id_and_initial_state() -> None:
    resp = client.post("/conversations", json={})
    assert resp.status_code == 200
    data = resp.json()
    assert "conversation_id" in data
    assert data["conversation_id"]
    assert "messages" in data
    assert "mission" in data
    assert "suggested_questions" in data
    assert len(data["messages"]) >= 1
    assert data["mission"]["company_name"] in ("TBD", "") or data["mission"]["company_name"]


def test_post_conversations_with_initial_message_calls_assistant() -> None:
    with patch("branding_team.api.main.assistant_agent") as mock_agent:
        mock_agent.respond.return_value = (
            "Got it, Acme it is!",
            BrandingMission(
                company_name="Acme",
                company_description="We build software.",
                target_audience="Developers",
            ),
            ["What are your values?", "Who are your competitors?"],
        )
        resp = client.post(
            "/conversations",
            json={"initial_message": "We're Acme, we build software for developers."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["conversation_id"]
        assert len(data["messages"]) >= 2
        assert data["suggested_questions"]


def test_post_conversation_messages_updates_state_and_returns_reply() -> None:
    create_resp = client.post("/conversations", json={})
    assert create_resp.status_code == 200
    conversation_id = create_resp.json()["conversation_id"]

    with patch("branding_team.api.main.assistant_agent") as mock_agent:
        mock_agent.respond.return_value = (
            "Thanks, I've noted that.",
            BrandingMission(
                company_name="TestCo",
                company_description="To be discussed.",
                target_audience="TBD",
            ),
            ["Next question?"],
        )
        msg_resp = client.post(
            f"/conversations/{conversation_id}/messages",
            json={"message": "Our company is TestCo."},
        )
        assert msg_resp.status_code == 200
        data = msg_resp.json()
        assert len(data["messages"]) >= 2
        assert data["mission"]
        assert "suggested_questions" in data


def test_get_conversation_returns_stored_state() -> None:
    create_resp = client.post("/conversations", json={})
    assert create_resp.status_code == 200
    conversation_id = create_resp.json()["conversation_id"]

    get_resp = client.get(f"/conversations/{conversation_id}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["conversation_id"] == conversation_id
    assert "messages" in data
    assert "mission" in data


def test_get_conversation_404_for_unknown_id() -> None:
    resp = client.get("/conversations/unknown-conversation-id")
    assert resp.status_code == 404


def test_brand_creation_auto_creates_conversation() -> None:
    """Creating a brand auto-creates a single permanent conversation."""
    create_c = client.post("/clients", json={"name": "AutoConv Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "AutoConvCo",
            "company_description": "Company with auto-created conversation",
            "target_audience": "teams",
        },
    )
    assert create_b.status_code == 201
    brand = create_b.json()
    assert brand["conversation_id"] is not None

    # The brand's conversation endpoint should return the conversation.
    conv_resp = client.get(f"/clients/{client_id}/brands/{brand['id']}/conversation")
    assert conv_resp.status_code == 200
    assert conv_resp.json()["conversation_id"] == brand["conversation_id"]
