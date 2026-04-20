"""Tests for the POST /assistant/launch endpoint.

The dispatcher calls into ``unified_api.main.app`` via an ASGI transport;
here we monkeypatch that module so the test never has to stand up the
full unified API. Postgres is faked via the in-memory helper used by the
rest of the team_assistant suite.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from fastapi.testclient import TestClient

from team_assistant.api import create_assistant_app
from team_assistant.config import TEAM_ASSISTANT_CONFIGS
from team_assistant.store import TeamAssistantConversationStore
from team_assistant.tests._fake_postgres import install_fake_postgres

# ---------------------------------------------------------------------------
# Stub "unified_api.main" that the dispatcher imports lazily.
#
# The dispatcher does ``from unified_api.main import app`` inside the call
# so we can hand it whatever ASGI app we want per-test.
# ---------------------------------------------------------------------------


class _UpstreamHarness:
    """Captures requests the dispatcher forwards, returns a scripted response."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response_status = 200
        self.response_body: dict[str, Any] = {"job_id": "job-1"}

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        assert scope["type"] == "http"
        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body_chunks.append(message.get("body") or b"")
                if not message.get("more_body"):
                    break

        headers = {k.decode(): v.decode() for k, v in scope.get("headers", [])}
        self.requests.append(
            {
                "method": scope["method"],
                "path": scope["path"],
                "query_string": scope.get("query_string", b"").decode(),
                "headers": headers,
                "body": b"".join(body_chunks),
            }
        )

        payload = json.dumps(self.response_body).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": self.response_status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": payload})


@pytest.fixture
def fake_pg(monkeypatch: pytest.MonkeyPatch) -> dict:
    return install_fake_postgres(monkeypatch)


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> _UpstreamHarness:
    harness = _UpstreamHarness()
    # Build a stub module the dispatcher can import.
    stub = types.ModuleType("unified_api.main")
    stub.app = harness  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "unified_api.main", stub)
    # Also make sure ``unified_api`` itself exists as a package entry so the
    # ``from unified_api.main import app`` resolves cleanly in isolation.
    if "unified_api" not in sys.modules:
        pkg = types.ModuleType("unified_api")
        monkeypatch.setitem(sys.modules, "unified_api", pkg)
    return harness


def _stub_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass TeamAssistantAgent's LLM initialisation.

    The endpoint only exercises ``check_readiness`` which is pure-Python,
    but constructing the agent default-imports strands. Stub the class to
    skip LLM wiring.
    """
    import team_assistant.api as api_mod

    class _NoLLMAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.required_field_keys = [f["key"] for f in kwargs.get("required_fields", [])]

        def check_readiness(self, context: dict[str, Any]) -> tuple[bool, list[str]]:
            missing = [k for k in self.required_field_keys if not context.get(k)]
            return (not missing, missing)

    monkeypatch.setattr(api_mod, "TeamAssistantAgent", _NoLLMAgent)
    monkeypatch.setattr(api_mod, "_agents", {})


def _seed_conversation(team_key: str, context: dict[str, Any]) -> tuple[TestClient, str]:
    config = TEAM_ASSISTANT_CONFIGS[team_key]
    app = create_assistant_app(config)
    client = TestClient(app)
    store = TeamAssistantConversationStore(team_key=team_key)
    cid = store.create(conversation_id=f"cid-{team_key}", context=context)
    return client, cid


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


def test_readiness_fails_returns_409_with_missing_fields(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    client, cid = _seed_conversation("blogging", context={})

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "missing_required_fields"
    assert detail["missing"] == ["brief"]
    # Upstream was never called.
    assert upstream.requests == []


def test_blogging_happy_path_links_job_to_conversation(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    upstream.response_body = {"job_id": "blog-42", "status": "queued"}
    client, cid = _seed_conversation(
        "blogging",
        context={"brief": "AI trends in 2026", "audience": "engineers"},
    )

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "ok": True,
        "job_id": "blog-42",
        "conversation_id": cid,
        "upstream_status": 200,
        "upstream_body": {"job_id": "blog-42", "status": "queued"},
    }

    # The dispatcher forwarded to the real run endpoint with the JSON body.
    assert len(upstream.requests) == 1
    req = upstream.requests[0]
    assert req["method"] == "POST"
    assert req["path"] == "/api/blogging/full-pipeline-async"
    assert json.loads(req["body"]) == {"brief": "AI trends in 2026", "audience": "engineers"}

    # Conversation is now linked to the job.
    store = TeamAssistantConversationStore(team_key="blogging")
    assert store.get_by_job_id("blog-42") == cid


def test_software_engineering_multipart_upload_for_spec_only_context(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    upstream.response_body = {"job_id": "se-99"}
    client, cid = _seed_conversation(
        "software_engineering",
        context={
            "spec": "Build a kanban board\nWith drag-and-drop",
            "tech_stack": "Angular + Django",
        },
    )

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200, resp.text
    assert resp.json()["job_id"] == "se-99"

    assert len(upstream.requests) == 1
    req = upstream.requests[0]
    assert req["path"] == "/api/software-engineering/run-team/upload"
    content_type = req["headers"].get("content-type", "")
    assert content_type.startswith("multipart/form-data")
    body = req["body"]
    # Verify the spec_file part is present and begins with the spec text.
    assert b'name="project_name"' in body
    assert b'name="spec_file"' in body
    assert b"Build a kanban board" in body
    assert b"## Tech Stack\nAngular + Django" in body


def test_software_engineering_json_path_when_repo_path_provided(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    upstream.response_body = {"job_id": "se-100"}
    client, cid = _seed_conversation(
        "software_engineering",
        context={"spec": "ignored when repo_path is set", "repo_path": "/workspaces/existing"},
    )

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200
    assert upstream.requests[0]["path"] == "/api/software-engineering/run-team"
    assert json.loads(upstream.requests[0]["body"]) == {"repo_path": "/workspaces/existing"}


def test_market_research_synchronous_has_null_job_id(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    # Market Research returns a TeamOutput-shape (no job_id).
    upstream.response_body = {"status": "ok", "summary": "..."}
    client, cid = _seed_conversation(
        "market_research",
        context={
            "product_concept": "Khala Assistant",
            "target_users": "engineering leads",
            "business_goal": "validate interest",
        },
    )

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["job_id"] is None
    # The full upstream body is surfaced so the dashboard can render it,
    # since there's no job to poll for synchronous teams.
    assert body["upstream_body"] == {"status": "ok", "summary": "..."}
    # And the conversation is NOT linked to any job.
    store = TeamAssistantConversationStore(team_key="market_research")
    assert store.get_by_job_id("") is None


def test_social_marketing_launch_injects_llm_model_name(
    fake_pg: dict,
    upstream: _UpstreamHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: upstream requires llm_model_name; assistant must supply it."""
    _stub_agent(monkeypatch)
    monkeypatch.setenv("LLM_MODEL", "llama3.1")
    upstream.response_body = {"job_id": "sm-1", "status": "queued", "message": "ok"}
    client, cid = _seed_conversation(
        "social_marketing",
        context={"client_id": "client-a", "brand_id": "brand-b"},
    )

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200, resp.text

    sent = json.loads(upstream.requests[0]["body"])
    assert sent["client_id"] == "client-a"
    assert sent["brand_id"] == "brand-b"
    assert sent["llm_model_name"] == "llama3.1"


def test_no_launch_spec_returns_400(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    client, cid = _seed_conversation("personal_assistant", context={"user_id": "u-1"})
    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 400
    assert "no launch workflow" in resp.json()["detail"].lower()
    assert upstream.requests == []


def test_upstream_5xx_surfaces_as_502(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    upstream.response_status = 500
    upstream.response_body = {"error": "internal_kaboom"}
    client, cid = _seed_conversation("blogging", context={"brief": "topic"})

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["error"] == "upstream_error"
    assert detail["upstream_status"] == 500
    assert detail["upstream_body"] == {"error": "internal_kaboom"}


def test_unknown_conversation_returns_404(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_agent(monkeypatch)
    client, _cid = _seed_conversation("blogging", context={"brief": "x"})
    resp = client.post("/launch?conversation_id=missing-cid")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Track B: cases for the newly onboarded teams
# ---------------------------------------------------------------------------


def test_branding_synchronous_returns_team_output(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Branding returns results inline with no job_id — the conversation must not be linked."""
    _stub_agent(monkeypatch)
    upstream.response_body = {"summary": "brand built", "artifacts": []}
    client, cid = _seed_conversation(
        "branding",
        context={
            "company_name": "Acme",
            "company_description": "sells anvils",
            "target_audience": "coyotes",
        },
    )

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["job_id"] is None
    assert body["upstream_body"] == {"summary": "brand built", "artifacts": []}
    assert upstream.requests[0]["path"] == "/api/branding/run"


def test_user_agent_founder_async_links_job(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /start now returns {job_id: ...} (renamed from run_id)."""
    _stub_agent(monkeypatch)
    upstream.response_body = {"job_id": "uaf-7", "status": "pending"}
    client, cid = _seed_conversation("user_agent_founder", context={})

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == "uaf-7"
    assert upstream.requests[0]["path"] == "/api/user-agent-founder/start"
    assert json.loads(upstream.requests[0]["body"]) == {}

    store = TeamAssistantConversationStore(team_key="user_agent_founder")
    assert store.get_by_job_id("uaf-7") == cid


def test_startup_advisor_400_no_launch_spec(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """startup_advisor has no required fields AND no launch_spec — readiness passes, 400 on launch."""
    _stub_agent(monkeypatch)
    client, cid = _seed_conversation("startup_advisor", context={})
    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 400
    assert "no launch workflow" in resp.json()["detail"].lower()
    assert upstream.requests == []


def test_investment_builder_sends_numeric_fields_as_numbers(
    fake_pg: dict, upstream: _UpstreamHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration check: the investment builder's numeric coercion survives JSON serialisation."""
    _stub_agent(monkeypatch)
    upstream.response_body = {"profile_id": "p-1", "ips": "..."}
    client, cid = _seed_conversation(
        "investment",
        context={
            "user_id": "u-7",
            "risk_tolerance": "aggressive",
            "max_drawdown_tolerance_pct": "30",
            "time_horizon_years": "15",
            "annual_gross_income": "250000",
        },
    )

    resp = client.post(f"/launch?conversation_id={cid}")
    assert resp.status_code == 200, resp.text

    sent = json.loads(upstream.requests[0]["body"])
    assert sent["max_drawdown_tolerance_pct"] == 30.0
    assert sent["time_horizon_years"] == 15
    assert sent["annual_gross_income"] == 250000.0
