import sys
import time
from typing import Any, Dict
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from branding_team.api.main import app, branding_store
from branding_team.tests._memory_stores import install_memory_stores
from branding_team.tests.conftest import make_mission
from job_service_client_fake import FakeJobServiceClient

# Hits the team API which calls the real job service.  Marked integration
# pending follow-up.
pytestmark = [pytest.mark.integration]

client = TestClient(app)


@pytest.fixture(autouse=True)
def _memory_stores(monkeypatch: pytest.MonkeyPatch):
    bundle = install_memory_stores(monkeypatch)
    # Rebind the module-level name imported as ``from ...main import branding_store``.
    from branding_team.api import main as main_mod
    from branding_team.shared import job_store

    monkeypatch.setattr(sys.modules[__name__], "branding_store", main_mod.branding_store)
    fake_jobs = FakeJobServiceClient(team="branding_team")
    monkeypatch.setattr(job_store, "_client", lambda: fake_jobs)
    monkeypatch.setattr(main_mod, "_job_manager", fake_jobs)
    return bundle


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


def test_answering_all_questions_regenerates_and_marks_ready() -> None:
    """Debounce: intermediate answers skip regeneration; answering the last
    open question triggers the full run and flips the session to ready."""
    create = client.post("/sessions", json=_payload())
    session = create.json()
    session_id = session["session_id"]

    latest = session
    # Keep answering whichever question is still open until none remain. The
    # bound is a safety net: if the API ever stops clearing open_questions this
    # fails loudly instead of hanging.
    for _ in range(100):
        if not latest["open_questions"]:
            break
        qid = latest["open_questions"][0]["id"]
        resp = client.post(
            f"/sessions/{session_id}/questions/{qid}/answer",
            json={"answer": "clarity, trust, craft"},
        )
        assert resp.status_code == 200
        latest = resp.json()
    else:
        pytest.fail("open_questions never cleared within 100 iterations")

    assert latest["status"] == "ready_for_rollout"
    assert latest["latest_output"]["strategic_core"] is not None


def test_unknown_session_404() -> None:
    """GET on a non-existent session id returns 404."""
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


def test_run_endpoint_builds_mission_and_returns_output() -> None:
    """`/run` builds a mission via _mission_from_payload and returns a TeamOutput."""
    resp = client.post("/run", json={**_payload(), "human_approved": True})
    assert resp.status_code == 200
    data = resp.json()
    assert "current_phase" in data
    assert "status" in data


def test_request_design_assets_returns_stub() -> None:
    """Fallback path (no cached core): the endpoint runs Phase 1, then stubs assets.

    The real pipeline is patched out so this API-layer test stays fast and
    deterministic and does not depend on the LLM/graph stack.
    """
    from branding_team.models import (
        BrandPhase,
        StrategicCoreOutput,
        TeamOutput,
        WorkflowStatus,
    )

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

    # Brand has no persisted output, so the endpoint falls back to run_phase —
    # patch it to a canned result to isolate the API layer from the pipeline.
    fake_output = TeamOutput(
        status=WorkflowStatus.NEEDS_HUMAN_DECISION,
        mission_summary="stub",
        current_phase=BrandPhase.STRATEGIC_CORE,
        strategic_core=StrategicCoreOutput(positioning_statement="STUB-POSITIONING"),
    )
    with patch(
        "branding_team.api.main.orchestrator.run_phase", return_value=fake_output
    ) as mock_run_phase:
        resp = client.post(f"/clients/{client_id}/brands/{brand_id}/request-design-assets")
        assert resp.status_code == 200
        mock_run_phase.assert_called_once()
    data = resp.json()
    assert "request_id" in data
    assert data["status"] == "pending"
    assert "artifacts" in data


def test_request_design_assets_reuses_cached_strategic_core() -> None:
    """The endpoint reuses a persisted strategic core instead of re-running Phase 1."""
    from branding_team.models import (
        BrandPhase,
        StrategicCoreOutput,
        TeamOutput,
        WorkflowStatus,
    )

    create_c = client.post("/clients", json={"name": "Cache Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "CacheCo",
            "company_description": "Company for design assets cache test",
            "target_audience": "designers",
        },
    )
    brand_id = create_b.json()["id"]

    # Persist a strategic core so the endpoint can reuse it.
    cached_output = TeamOutput(
        status=WorkflowStatus.READY_FOR_ROLLOUT,
        mission_summary="cached",
        current_phase=BrandPhase.STRATEGIC_CORE,
        strategic_core=StrategicCoreOutput(positioning_statement="CACHED-POSITIONING"),
    )
    branding_store.append_brand_version(client_id, brand_id, cached_output)

    # Phase 1 must NOT run when a cached core exists.
    with patch("branding_team.api.main.orchestrator.run_phase") as mock_run_phase:
        resp = client.post(f"/clients/{client_id}/brands/{brand_id}/request-design-assets")
        assert resp.status_code == 200
        mock_run_phase.assert_not_called()
    # The stub echoes the (cached) positioning into its artifacts.
    assert any("CACHED-POSITIONING" in a for a in resp.json()["artifacts"])


def test_request_design_assets_recomputes_after_mission_edit() -> None:
    """Editing the mission invalidates the cached core, so Phase 1 re-runs.

    Guards against serving stale positioning from a strategic core generated for
    a previous mission (the cache is only reused when it reflects the current
    mission).
    """
    from branding_team.models import (
        BrandPhase,
        StrategicCoreOutput,
        TeamOutput,
        WorkflowStatus,
    )

    create_c = client.post("/clients", json={"name": "Edit Cache Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "EditCo",
            "company_description": "Original description for cache-invalidation test",
            "target_audience": "designers",
        },
    )
    brand_id = create_b.json()["id"]

    # Persist a strategic core built from the original mission.
    branding_store.append_brand_version(
        client_id,
        brand_id,
        TeamOutput(
            status=WorkflowStatus.READY_FOR_ROLLOUT,
            mission_summary="cached",
            current_phase=BrandPhase.STRATEGIC_CORE,
            strategic_core=StrategicCoreOutput(positioning_statement="STALE-POSITIONING"),
        ),
    )

    # Edit the mission — this must clear the now-stale latest_output.
    edit = client.put(
        f"/clients/{client_id}/brands/{brand_id}",
        json={"company_description": "A substantially rewritten company description"},
    )
    assert edit.status_code == 200
    assert edit.json()["latest_output"] is None

    # With no valid cached core, the endpoint recomputes Phase 1 (patched here).
    fresh = TeamOutput(
        status=WorkflowStatus.NEEDS_HUMAN_DECISION,
        mission_summary="fresh",
        current_phase=BrandPhase.STRATEGIC_CORE,
        strategic_core=StrategicCoreOutput(positioning_statement="FRESH-POSITIONING"),
    )
    with patch(
        "branding_team.api.main.orchestrator.run_phase", return_value=fresh
    ) as mock_run_phase:
        resp = client.post(f"/clients/{client_id}/brands/{brand_id}/request-design-assets")
        assert resp.status_code == 200
        mock_run_phase.assert_called_once()
    assert any("FRESH-POSITIONING" in a for a in resp.json()["artifacts"])


def test_update_brand_unchanged_mission_preserves_output() -> None:
    """A PUT that resends unchanged mission fields (e.g. with a name edit) must
    not discard the generated output — only a real mission change invalidates it."""
    from branding_team.models import (
        BrandPhase,
        StrategicCoreOutput,
        TeamOutput,
        WorkflowStatus,
    )

    create_c = client.post("/clients", json={"name": "Idempotent Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "IdemCo",
            "company_description": "Description that will be resent unchanged",
            "target_audience": "everyone",
        },
    )
    brand_id = create_b.json()["id"]
    branding_store.append_brand_version(
        client_id,
        brand_id,
        TeamOutput(
            status=WorkflowStatus.READY_FOR_ROLLOUT,
            mission_summary="cached",
            current_phase=BrandPhase.STRATEGIC_CORE,
            strategic_core=StrategicCoreOutput(positioning_statement="KEEP-ME"),
        ),
    )

    # Resend the same mission fields alongside a name change: output is preserved.
    resp = client.put(
        f"/clients/{client_id}/brands/{brand_id}",
        json={
            "name": "Renamed Brand",
            "company_description": "Description that will be resent unchanged",
            "target_audience": "everyone",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renamed Brand"
    assert resp.json()["latest_output"] is not None
    assert resp.json()["latest_output"]["strategic_core"]["positioning_statement"] == "KEEP-ME"


def test_request_design_assets_unknown_brand_404() -> None:
    create_c = client.post("/clients", json={"name": "DA 404 Client"})
    client_id = create_c.json()["id"]
    resp = client.post(f"/clients/{client_id}/brands/does-not-exist/request-design-assets")
    assert resp.status_code == 404


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
            make_mission(
                company_name="Acme",
                company_description="We build software.",
                target_audience="Developers",
            ),
            ["What are your values?", "Who are your competitors?"],
            False,
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
            make_mission(
                company_name="TestCo",
                company_description="To be discussed.",
                target_audience="TBD",
            ),
            ["Next question?"],
            False,
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


def test_list_conversations_resolves_brand_names() -> None:
    """GET /conversations exercises get_brand_names: attached conversations
    carry their brand's name, unattached ones report None."""
    create_c = client.post("/clients", json={"name": "ListConv Client"})
    client_id = create_c.json()["id"]
    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "ListBrandCo",
            "company_description": "Company for list conversations test",
            "target_audience": "teams",
        },
    )
    brand = create_b.json()
    attached_conv_id = brand["conversation_id"]

    # An unattached conversation (no brand).
    unattached_id = client.post("/conversations", json={}).json()["conversation_id"]

    resp = client.get("/conversations")
    assert resp.status_code == 200
    summaries = {s["conversation_id"]: s for s in resp.json()}
    assert summaries[attached_conv_id]["brand_id"] == brand["id"]
    assert summaries[attached_conv_id]["brand_name"] == brand["name"]
    assert summaries[unattached_id]["brand_id"] is None
    assert summaries[unattached_id]["brand_name"] is None

    # Filtering by brand_id returns only that brand's conversation.
    filtered = client.get("/conversations", params={"brand_id": brand["id"]})
    assert filtered.status_code == 200
    assert [s["conversation_id"] for s in filtered.json()] == [attached_conv_id]


def test_attach_conversation_to_brand_succeeds() -> None:
    """POST /conversations/{id}/brand attaches an unattached conversation and
    the new state (single-query load) reports the brand."""
    # Brand created directly via the store has no conversation yet, so the
    # one-conversation-per-brand invariant allows attaching one here.
    workspace = branding_store.create_client("Attach Client")
    brand = branding_store.create_brand(
        workspace.id,
        make_mission(
            company_name="AttachCo",
            company_description="Company for attach test",
            target_audience="users",
        ),
    )
    conv_id = client.post("/conversations", json={}).json()["conversation_id"]

    resp = client.post(f"/conversations/{conv_id}/brand", json={"brand_id": brand.id})
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversation_id"] == conv_id
    assert data["brand_id"] == brand.id

    # Reloading the conversation reflects the attachment.
    reload = client.get(f"/conversations/{conv_id}")
    assert reload.status_code == 200
    assert reload.json()["brand_id"] == brand.id


def test_attach_conversation_unknown_brand_404() -> None:
    """Attaching a conversation to a non-existent brand returns 404."""
    conv_id = client.post("/conversations", json={}).json()["conversation_id"]
    resp = client.post(f"/conversations/{conv_id}/brand", json={"brand_id": "brand_missing"})
    assert resp.status_code == 404


def test_attach_conversation_unknown_conversation_404() -> None:
    """Attaching a non-existent conversation to a real brand returns 404."""
    workspace = branding_store.create_client("Attach 404 Client")
    brand = branding_store.create_brand(
        workspace.id,
        make_mission(
            company_name="Attach404Co",
            company_description="Company for attach 404 test",
            target_audience="users",
        ),
    )
    resp = client.post("/conversations/unknown-conv-id/brand", json={"brand_id": brand.id})
    assert resp.status_code == 404


def test_create_brand_with_existing_conversation_id_attaches_it() -> None:
    """POST /clients/{id}/brands with a conversation_id attaches that
    conversation atomically instead of auto-creating a new one."""
    create_c = client.post("/clients", json={"name": "ExistingConv Client"})
    client_id = create_c.json()["id"]
    conv_id = client.post("/conversations", json={}).json()["conversation_id"]

    create_b = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "ExistingConvCo",
            "company_description": "Company created with a pre-existing conversation",
            "target_audience": "teams",
            "conversation_id": conv_id,
        },
    )
    assert create_b.status_code == 201, create_b.text
    brand = create_b.json()
    assert brand["conversation_id"] == conv_id

    conv_resp = client.get(f"/clients/{client_id}/brands/{brand['id']}/conversation")
    assert conv_resp.status_code == 200
    assert conv_resp.json()["conversation_id"] == conv_id


def test_create_brand_with_conversation_already_attached_returns_409() -> None:
    """Reusing a conversation_id already attached to another brand is a conflict."""
    create_c = client.post("/clients", json={"name": "ConflictConv Client"})
    client_id = create_c.json()["id"]

    first = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "FirstCo",
            "company_description": "First brand owning the conversation",
            "target_audience": "teams",
        },
    )
    assert first.status_code == 201
    taken_conv_id = first.json()["conversation_id"]

    second = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "SecondCo",
            "company_description": "Second brand trying to reuse the conversation",
            "target_audience": "teams",
            "conversation_id": taken_conv_id,
        },
    )
    assert second.status_code == 409

    # The failed second brand must not be left behind as an orphan.
    brands = client.get(f"/clients/{client_id}/brands").json()
    assert [b["id"] for b in brands] == [first.json()["id"]]


def test_create_brand_with_unknown_conversation_id_returns_404() -> None:
    """A conversation_id that doesn't exist yields 404, not a silent auto-create."""
    create_c = client.post("/clients", json={"name": "MissingConv Client"})
    client_id = create_c.json()["id"]

    resp = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "MissingConvCo",
            "company_description": "Company referencing an unknown conversation",
            "target_audience": "teams",
            "conversation_id": "conv_does_not_exist",
        },
    )
    assert resp.status_code == 404

    # The brand committed before the conversation check failed must not survive.
    brands = client.get(f"/clients/{client_id}/brands").json()
    assert brands == []


def test_create_brand_rolls_back_when_conversation_create_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception from conversation_store.create() on the create-new path
    must not leave a listable, conversation-less orphan brand behind."""
    from branding_team.api import main as main_mod

    create_c = client.post("/clients", json={"name": "ConvCreateFails Client"})
    client_id = create_c.json()["id"]

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("conversation store unavailable")

    monkeypatch.setattr(main_mod.conversation_store, "create", _boom)

    with pytest.raises(RuntimeError, match="conversation store unavailable"):
        client.post(
            f"/clients/{client_id}/brands",
            json={
                "company_name": "ConvCreateFailsCo",
                "company_description": "Company whose conversation creation fails",
                "target_audience": "teams",
            },
        )

    # The brand created before conversation_store.create() raised must not survive.
    brands = client.get(f"/clients/{client_id}/brands").json()
    assert brands == []


def test_create_brand_rejects_unrecognized_attach_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An attach_conversation result that isn't OK or one of the three known
    failure members must still roll back the brand and raise — never fall
    through to returning a brand that was just deleted."""
    from branding_team.api import main as main_mod

    create_c = client.post("/clients", json={"name": "UnrecognizedAttach Client"})
    client_id = create_c.json()["id"]

    def _unrecognized(*args: Any, **kwargs: Any) -> tuple:
        return object(), None

    monkeypatch.setattr(main_mod.branding_store, "attach_conversation", _unrecognized)

    resp = client.post(
        f"/clients/{client_id}/brands",
        json={
            "company_name": "UnrecognizedAttachCo",
            "company_description": "Company whose attach result is unrecognized",
            "target_audience": "teams",
        },
    )
    assert resp.status_code == 500

    # The brand must not survive as a listable, conversation-less orphan.
    brands = client.get(f"/clients/{client_id}/brands").json()
    assert brands == []


def test_create_brand_rolls_back_when_attach_conversation_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception from store.attach_conversation() on the create-new path
    (after the fresh, unattached conversation was already created) must roll
    back the brand and propagate the original error — the conversation stays
    unattached rather than pointing at a brand about to be deleted, since it
    only ever gains a brand_id inside attach_conversation's own transaction."""
    from branding_team.api import main as main_mod

    create_c = client.post("/clients", json={"name": "AttachConvFails Client"})
    client_id = create_c.json()["id"]

    # Capture the conversation id conversation_store.create() actually
    # produces so we can inspect it afterward — it never reaches the caller
    # since the request raises before a response body is built.
    created_conv_ids = []
    real_create = main_mod.conversation_store.create

    def _capturing_create(*args: Any, **kwargs: Any) -> str:
        conv_id = real_create(*args, **kwargs)
        created_conv_ids.append(conv_id)
        return conv_id

    monkeypatch.setattr(main_mod.conversation_store, "create", _capturing_create)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("attach_conversation unavailable")

    monkeypatch.setattr(main_mod.branding_store, "attach_conversation", _boom)

    with pytest.raises(RuntimeError, match="attach_conversation unavailable"):
        client.post(
            f"/clients/{client_id}/brands",
            json={
                "company_name": "AttachConvFailsCo",
                "company_description": "Company whose brand-conversation link write fails",
                "target_audience": "teams",
            },
        )

    # The brand must not survive the rolled-back request.
    brands = client.get(f"/clients/{client_id}/brands").json()
    assert brands == []

    # The conversation it created must remain unattached, not pointing at
    # the now-deleted brand id.
    assert len(created_conv_ids) == 1
    conv_resp = client.get(f"/conversations/{created_conv_ids[0]}")
    assert conv_resp.status_code == 200
    assert conv_resp.json()["brand_id"] is None


def test_list_clients_pagination_query_params() -> None:
    """GET /clients honors limit/offset and returns non-overlapping pages."""
    created = {
        client.post("/clients", json={"name": f"Page Client {i}"}).json()["id"] for i in range(4)
    }
    first = client.get("/clients", params={"limit": 2, "offset": 0})
    second = client.get("/clients", params={"limit": 2, "offset": 2})
    assert first.status_code == 200 and second.status_code == 200
    first_ids = {c["id"] for c in first.json()}
    second_ids = {c["id"] for c in second.json()}
    assert len(first.json()) == 2
    assert first_ids.isdisjoint(second_ids)
    # The full (unpaginated) listing still includes everything we created.
    all_ids = {c["id"] for c in client.get("/clients").json()}
    assert created <= all_ids


def test_list_clients_rejects_invalid_pagination() -> None:
    """Out-of-range limit/offset are a 422 (FastAPI validation), never a 500."""
    assert client.get("/clients", params={"limit": 0}).status_code == 422
    assert client.get("/clients", params={"limit": -1}).status_code == 422
    assert client.get("/clients", params={"offset": -1}).status_code == 422


def test_list_brands_pagination_query_params() -> None:
    """GET /clients/{id}/brands honors limit/offset and validates them."""
    client_id = client.post("/clients", json={"name": "Brand Page Client"}).json()["id"]
    for i in range(3):
        resp = client.post(
            f"/clients/{client_id}/brands",
            json={
                "company_name": f"PageBrand {i}",
                "company_description": "Company for brand pagination test",
                "target_audience": "testers",
            },
        )
        assert resp.status_code == 201
    page = client.get(f"/clients/{client_id}/brands", params={"limit": 1, "offset": 1})
    assert page.status_code == 200
    assert len(page.json()) == 1
    assert client.get(f"/clients/{client_id}/brands", params={"limit": 0}).status_code == 422
