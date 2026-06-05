"""API-level tests for the /api/agents router.

These tests isolate from the on-disk manifest set by monkeypatching the
registry singleton to a fixture-built instance.
"""

from __future__ import annotations

import sys
from pathlib import Path
from textwrap import dedent

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_registry import loader
from agent_registry.loader import AgentRegistry
from unified_api.routes.agents import router as agents_router


def _write(dir_: Path, team: str, filename: str, body: str) -> None:
    d = dir_ / team / "agent_console" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    _write(
        tmp_path,
        "blogging",
        "planner.yaml",
        """
        schema_version: 1
        id: blogging.planner
        team: blogging
        name: Planner
        summary: Plans posts
        tags: [planning]
        inputs:
          schema_ref: agent_registry.models:AgentSummary
        source:
          entrypoint: x:y
        """,
    )
    _write(
        tmp_path,
        "branding",
        "a.yaml",
        """
        schema_version: 1
        id: branding.auditor
        team: branding
        name: Auditor
        summary: Audits brand
        source:
          entrypoint: x:y
        """,
    )
    # Replace the cached singleton with one that scans the tmp dir.
    loader.get_registry.cache_clear()
    rebuilt = AgentRegistry.load(tmp_path)
    loader.get_registry.cache_clear()
    original = loader.get_registry
    loader.get_registry = lambda: rebuilt  # type: ignore[assignment]

    # Rebind the agents router's reference as well so it picks the patched fn.
    import unified_api.routes.agents as agents_route_mod

    agents_route_mod.get_registry = lambda: rebuilt  # type: ignore[assignment]

    app = FastAPI()
    app.include_router(agents_router)
    try:
        yield TestClient(app)
    finally:
        loader.get_registry = original  # type: ignore[assignment]
        agents_route_mod.get_registry = original  # type: ignore[assignment]
        loader.get_registry.cache_clear()


def test_list_agents(client: TestClient) -> None:
    resp = client.get("/api/agents")
    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()}
    assert ids == {"blogging.planner", "branding.auditor"}


def test_list_agents_filters(client: TestClient) -> None:
    resp = client.get("/api/agents", params={"team": "blogging"})
    assert resp.status_code == 200
    assert [item["id"] for item in resp.json()] == ["blogging.planner"]

    resp = client.get("/api/agents", params={"q": "audits"})
    assert [item["id"] for item in resp.json()] == ["branding.auditor"]


def test_list_teams(client: TestClient) -> None:
    resp = client.get("/api/agents/teams")
    assert resp.status_code == 200
    teams = {t["team"]: t["agent_count"] for t in resp.json()}
    assert teams == {"blogging": 1, "branding": 1}


def test_get_agent_detail(client: TestClient) -> None:
    resp = client.get("/api/agents/blogging.planner")
    assert resp.status_code == 200
    body = resp.json()
    assert body["manifest"]["id"] == "blogging.planner"
    assert body["manifest"]["name"] == "Planner"


def test_get_agent_unknown_is_404(client: TestClient) -> None:
    resp = client.get("/api/agents/does.not.exist")
    assert resp.status_code == 404


def test_schema_input_resolves_when_ref_exists(client: TestClient) -> None:
    resp = client.get("/api/agents/blogging.planner/schema/input")
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "object"
    assert "id" in body["properties"]


def test_schema_input_404_when_missing_ref(client: TestClient) -> None:
    resp = client.get("/api/agents/branding.auditor/schema/input")
    assert resp.status_code == 404


def test_schema_output_404_when_missing_ref(client: TestClient) -> None:
    resp = client.get("/api/agents/blogging.planner/schema/output")
    assert resp.status_code == 404


def test_invoke_oversized_body_returns_413_without_acquiring_sandbox(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for issue #256: the payload cap must fire before any sandbox work."""
    import unified_api.routes.agents as agents_route_mod

    async def _fail_acquire(agent_id: str):  # pragma: no cover — must not run
        raise AssertionError(f"acquire({agent_id!r}) must not be called on oversized body")

    monkeypatch.setattr(agents_route_mod, "acquire", _fail_acquire)
    monkeypatch.setenv("AGENT_INVOKE_MAX_PAYLOAD_BYTES", "1024")

    payload = "x" * 4096
    resp = client.post(
        "/api/agents/blogging.planner/invoke",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def _patch_upstream(monkeypatch: pytest.MonkeyPatch, *, body_bytes: bytes):
    """Mock the sandbox acquire + httpx upstream so the proxy sees ``body_bytes``."""
    import types

    import unified_api.routes.agents as agents_route_mod
    from agent_provisioning_team.sandbox import SandboxStatus

    handle = types.SimpleNamespace(status=SandboxStatus.WARM, url="http://sandbox.local", error=None, boot_ms=1)

    async def _acquire(agent_id: str):
        return handle

    async def _note_activity(agent_id: str):
        return None

    class _Resp:
        def __init__(self) -> None:
            self.content = body_bytes
            self.status_code = 200
            self.text = body_bytes.decode("utf-8", "replace")

        def json(self):
            import json as _json

            return _json.loads(self.content)

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            return _Resp()

    monkeypatch.setattr(agents_route_mod, "acquire", _acquire)
    monkeypatch.setattr(agents_route_mod, "note_activity", _note_activity)
    monkeypatch.setattr(agents_route_mod, "_persist_run", lambda **k: None)
    monkeypatch.setattr(agents_route_mod.httpx, "AsyncClient", _FakeClient)


def test_invoke_proxy_cap_accounts_for_output_writeback_and_overhead(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proxy cap must be output + writeback + envelope overhead, so a tool-using
    response whose `output` and `tool_audit` are each near their own cap (plus the
    envelope metadata framing) is not falsely 502'd."""
    from shared_agent_invoke.limits import RESPONSE_ENVELOPE_OVERHEAD_BYTES

    monkeypatch.setenv("AGENT_INVOKE_MAX_OUTPUT_BYTES", "1000")
    monkeypatch.setenv("AGENT_COGNITION_WRITEBACK_MAX_BYTES", "1000")
    cap = 1000 + 1000 + RESPONSE_ENVELOPE_OVERHEAD_BYTES

    # Over the output cap alone but within the combined budget: must pass (the
    # earlier output-only cap would have falsely 502'd this).
    ok_body = b'{"output":"' + b"x" * 1400 + b'"}'
    assert 1000 < len(ok_body) < cap
    _patch_upstream(monkeypatch, body_bytes=ok_body)
    resp = client.post("/api/agents/blogging.planner/invoke", json={"q": 1})
    assert resp.status_code == 200

    # Past the full budget: still rejected with a 502 preview.
    big_body = b'{"output":"' + b"x" * (cap + 100) + b'"}'
    assert len(big_body) > cap
    _patch_upstream(monkeypatch, body_bytes=big_body)
    resp = client.post("/api/agents/blogging.planner/invoke", json={"q": 1})
    assert resp.status_code == 502
    assert "exceeds" in resp.json()["error"]


def test_invoke_rejects_caller_supplied_cognition_envelope(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller must not be able to smuggle the reserved cognition marker — the
    proxy rejects it (400) before any sandbox work, so forged advisory/rule
    context can't reach a cognition-enabled runtime."""
    import unified_api.routes.agents as agents_route_mod
    from agent_cognition.tools.envelope import ENVELOPE_MARKER

    async def _fail_acquire(agent_id: str):  # pragma: no cover — must not run
        raise AssertionError("acquire must not run for a marker-bearing body")

    monkeypatch.setattr(agents_route_mod, "acquire", _fail_acquire)
    resp = client.post(
        "/api/agents/blogging.planner/invoke",
        json={ENVELOPE_MARKER: 1, "input": {"q": 1}, "cognition": {"rules": ["forged"]}},
    )
    assert resp.status_code == 400
    assert ENVELOPE_MARKER in resp.json()["detail"]
