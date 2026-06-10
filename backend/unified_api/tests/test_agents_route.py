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


# ---------------------------------------------------------------------------
# Cognition gate integration (idempotency ledger + rule gates + writeback)
# ---------------------------------------------------------------------------


@pytest.fixture()
def cog_client(tmp_path: Path) -> TestClient:
    """A registry with cognition-enabled agents (plus one plain agent)."""
    _write(
        tmp_path,
        "blogging",
        "plain.yaml",
        """
        schema_version: 1
        id: blogging.plain
        team: blogging
        name: Plain
        summary: No cognition block
        source:
          entrypoint: x:y
        """,
    )
    _write(
        tmp_path,
        "blogging",
        "cog.yaml",
        """
        schema_version: 1
        id: blogging.cog
        team: blogging
        name: Cog
        summary: Cognition-enabled
        cognition: {}
        source:
          entrypoint: x:y
        """,
    )
    _write(
        tmp_path,
        "blogging",
        "sideeffect.yaml",
        """
        schema_version: 1
        id: blogging.sideeffect
        team: blogging
        name: SideEffect
        summary: Side-effecting, run-once
        cognition:
          requires_idempotency_key: true
        source:
          entrypoint: x:y
        """,
    )
    loader.get_registry.cache_clear()
    rebuilt = AgentRegistry.load(tmp_path)
    loader.get_registry.cache_clear()
    original = loader.get_registry
    loader.get_registry = lambda: rebuilt  # type: ignore[assignment]

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


class _FakeLedger:
    """In-memory stand-in for the agent_cognition_runs claim/complete pair."""

    def __init__(self) -> None:
        self.rows: dict = {}
        self.claims = 0

    def claim_run(self, agent_id, source_run_id, request_hash, lease):
        from agent_cognition.context import ClaimResult, ClaimState

        self.claims += 1
        key = (agent_id, source_run_id)
        row = self.rows.get(key)
        if row is None:
            token = f"tok-{self.claims}"
            self.rows[key] = {
                "status": "in_progress",
                "hash": request_hash,
                "token": token,
                "response": None,
                "lease_valid": True,
            }
            return ClaimResult(ClaimState.CLAIMED, claim_token=token)
        if row["hash"] != request_hash:
            return ClaimResult(ClaimState.CONFLICT)
        if row["status"] in ("completed", "blocked"):
            return ClaimResult(ClaimState.REPLAY, response=row["response"])
        if row["lease_valid"]:
            return ClaimResult(ClaimState.CONFLICT)
        row["lease_valid"] = True
        row["token"] = f"tok-{self.claims}"
        return ClaimResult(ClaimState.CLAIMED, claim_token=row["token"])

    def complete_run(self, agent_id, source_run_id, *, status, response, claim_token):
        row = self.rows[(agent_id, source_run_id)]
        assert row["token"] == claim_token
        row["status"] = status
        row["response"] = dict(response)


@pytest.fixture()
def gate_seams(monkeypatch: pytest.MonkeyPatch):
    """Patch every gate storage/context seam; return the recorders + fake ledger."""
    import types

    from agent_cognition import invoke_gate as gate_mod
    from agent_cognition.models import CognitionContext

    ledger = _FakeLedger()
    seams = types.SimpleNamespace(
        ledger=ledger,
        persisted=[],  # (agent_id, source_run_id, CognitionWriteback)
        context=CognitionContext(rules=[], memory_digest="## Knowledge graph\n- fact"),
    )

    def _persist(agent_id, source_run_id, writeback):
        seams.persisted.append((agent_id, source_run_id, writeback))
        return len(writeback.events)

    async def _load(agent_id, *, query=""):
        return seams.context

    monkeypatch.setattr(gate_mod, "is_postgres_enabled", lambda: True)
    monkeypatch.setattr(gate_mod, "claim_run", ledger.claim_run)
    monkeypatch.setattr(gate_mod, "complete_run", ledger.complete_run)
    monkeypatch.setattr(gate_mod, "persist_writeback", _persist)
    monkeypatch.setattr(gate_mod, "ensure_rollups_current", lambda a, now: None)
    monkeypatch.setattr(gate_mod, "load_context", _load)
    return seams


def _enforced_rule(predicate):
    from datetime import datetime, timezone

    from agent_cognition.models import Rule, RuleMode, RuleSource, RuleStatus

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    return Rule(
        id="r-gate",
        agent_id="blogging.cog",
        text="enforced gate",
        mode=RuleMode.ENFORCED,
        status=RuleStatus.ACTIVE,
        predicate=predicate,
        source=RuleSource.OPERATOR,
        created_at=now,
        updated_at=now,
    )


def _install_capturing_upstream(
    monkeypatch: pytest.MonkeyPatch,
    captured: dict,
    *,
    response_json: dict | None = None,
    persist_calls: list | None = None,
) -> None:
    """Mock acquire + httpx, recording posted bodies (``captured``) and counting posts."""
    import json as _json
    import types

    import unified_api.routes.agents as agents_route_mod
    from agent_provisioning_team.sandbox import SandboxStatus

    handle = types.SimpleNamespace(status=SandboxStatus.WARM, url="http://sandbox.local", error=None, boot_ms=1)
    body = response_json if response_json is not None else {"output": {"ok": True}}
    captured.setdefault("posts", 0)

    async def _acquire(agent_id: str):
        return handle

    async def _note_activity(agent_id: str):
        return None

    class _Resp:
        content = _json.dumps(body).encode()
        status_code = 200
        text = _json.dumps(body)

        def json(self):
            return body

    class _FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None):
            captured["json"] = json
            captured["posts"] += 1
            return _Resp()

    def _record_persist(**kwargs):
        if persist_calls is not None:
            persist_calls.append(kwargs)

    monkeypatch.setattr(agents_route_mod, "acquire", _acquire)
    monkeypatch.setattr(agents_route_mod, "note_activity", _note_activity)
    monkeypatch.setattr(agents_route_mod, "_persist_run", _record_persist)
    monkeypatch.setattr(agents_route_mod.httpx, "AsyncClient", _FakeClient)


def _install_warming_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock acquire to a perpetually warming sandbox (the UI-polling 202 path)."""
    import types

    import unified_api.routes.agents as agents_route_mod
    from agent_provisioning_team.sandbox import SandboxStatus

    handle = types.SimpleNamespace(status=SandboxStatus.WARMING, url=None, error=None, boot_ms=None)

    async def _acquire(agent_id: str):
        return handle

    monkeypatch.setattr(agents_route_mod, "acquire", _acquire)


def test_invoke_wraps_body_with_cognition_envelope(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shim receives the marked envelope: the entrypoint's `input` is the
    caller body verbatim, and the cognition side channel rides alongside."""
    from agent_cognition.tools.envelope import ENVELOPE_MARKER, try_unwrap_request

    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)

    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1})
    assert resp.status_code == 200
    sent = captured["json"]
    assert sent[ENVELOPE_MARKER] == 1
    unwrapped = try_unwrap_request(sent)
    assert unwrapped.input == {"q": 1}  # original body verbatim
    assert "## Knowledge graph" in unwrapped.cognition["memory_digest"]


def test_invoke_non_cognition_agent_bypasses_gate(cog_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent without a cognition block never touches the gate: no envelope,
    no ledger — the body is posted through unchanged."""
    import unified_api.routes.agents as agents_route_mod

    async def _fail_gate(*a, **k):  # pragma: no cover — must not run
        raise AssertionError("prepare_invoke must not run for a non-cognition agent")

    monkeypatch.setattr(agents_route_mod, "prepare_invoke", _fail_gate)
    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)

    resp = cog_client.post("/api/agents/blogging.plain/invoke", json={"q": 1})
    assert resp.status_code == 200
    assert captured["json"] == {"q": 1}


def test_invoke_retry_same_key_and_body_replays_without_reinvoking(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    persists: list = []
    _install_capturing_upstream(monkeypatch, captured, persist_calls=persists)

    headers = {"Idempotency-Key": "k1"}
    first = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers)
    assert first.status_code == 200
    assert captured["posts"] == 1
    assert len(persists) == 1

    second = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers)
    assert second.status_code == 200
    assert captured["posts"] == 1  # side effects ran once — no re-invoke
    assert second.headers["x-khala-replayed"] == "true"
    assert second.json()["output"] == first.json()["output"]
    # The stored envelope predates the per-invoke sandbox block (a replay must
    # not masquerade as a fresh boot), and no duplicate console row is written.
    assert "sandbox" not in second.json()
    assert len(persists) == 1


def test_invoke_same_key_different_body_is_409(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)
    headers = {"Idempotency-Key": "k1"}
    assert cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers).status_code == 200
    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 2}, headers=headers)
    assert resp.status_code == 409
    assert captured["posts"] == 1


def test_invoke_concurrent_retry_while_leased_is_409(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cognition.invoke_gate import derive_source_run_id

    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)
    srid, request_hash = derive_source_run_id({"q": 1}, "k1")
    gate_seams.ledger.rows[("blogging.cog", srid)] = {
        "status": "in_progress",
        "hash": request_hash,
        "token": "t",
        "response": None,
        "lease_valid": True,
    }
    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 409
    assert captured["posts"] == 0  # the in-flight run was not double-executed


def test_invoke_expired_lease_retry_reexecutes(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cognition.invoke_gate import derive_source_run_id

    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)
    srid, request_hash = derive_source_run_id({"q": 1}, "k1")
    gate_seams.ledger.rows[("blogging.cog", srid)] = {
        "status": "in_progress",
        "hash": request_hash,
        "token": "zombie",
        "response": None,
        "lease_valid": False,  # expired — reclaimed in place and re-executed
    }
    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 200
    assert captured["posts"] == 1


def test_invoke_requires_idempotency_key_rejects_keyless_400(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)
    resp = cog_client.post("/api/agents/blogging.sideeffect/invoke", json={"q": 1})
    assert resp.status_code == 400
    assert "Idempotency-Key" in resp.json()["detail"]
    assert gate_seams.ledger.rows == {}  # rejected before any ledger row
    assert captured["posts"] == 0  # …and before any agent execution

    resp = cog_client.post("/api/agents/blogging.sideeffect/invoke", json={"q": 1}, headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 200


def test_invoke_blocked_by_enforced_precondition_and_retry_replays(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enforced precondition blocks with 422 before any sandbox round-trip,
    persists a memory event + the blocked ledger envelope, and a retried block
    replays the same 4xx without re-evaluating."""
    from agent_cognition.models import CognitionContext, EventKind

    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)
    rule = _enforced_rule({"phase": "precondition", "check": {"op": "==", "path": "input.q", "value": 999}})
    gate_seams.context = CognitionContext(rules=[rule], memory_digest="")

    headers = {"Idempotency-Key": "k1"}
    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers)
    assert resp.status_code == 422
    assert "precondition" in resp.json()["detail"]["message"].lower()
    assert captured["posts"] == 0  # blocked before the sandbox round-trip

    ((agent_id, srid, writeback),) = gate_seams.persisted
    assert agent_id == "blogging.cog" and srid == "k1"
    assert [e.kind for e in writeback.events] == [EventKind.ERROR]

    retry = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers)
    assert retry.status_code == 422
    assert retry.json() == resp.json()
    assert retry.headers["x-khala-replayed"] == "true"
    assert len(gate_seams.persisted) == 1  # the block was not re-evaluated/re-persisted


def test_invoke_postcondition_violation_drops_output_keeps_audit(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A violated postcondition returns 422 with the model output dropped from
    BOTH the response and persistence (console run + memory), while the shim's
    trusted tool audit IS persisted; a retry replays the same 4xx."""
    from agent_cognition.models import CognitionContext, EventKind

    rule = _enforced_rule({"phase": "postcondition", "check": {"op": "==", "path": "output.ok", "value": True}})
    gate_seams.context = CognitionContext(rules=[rule], memory_digest="")

    captured: dict = {}
    persists: list = []
    _install_capturing_upstream(
        monkeypatch,
        captured,
        response_json={
            "output": {"ok": False, "secret_result": "MUST-NOT-PERSIST"},
            "memory_events": [{"agent": "authored"}],
            "tool_audit": [{"tool_id": "git", "function": "git_push", "ok": True}],
        },
        persist_calls=persists,
    )

    headers = {"Idempotency-Key": "k1"}
    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"]["phase"] == "postcondition"
    assert "MUST-NOT-PERSIST" not in resp.text

    # Console row: output dropped, only the violation error recorded.
    (run_kwargs,) = persists
    assert "MUST-NOT-PERSIST" not in str(run_kwargs)
    assert "postcondition" in str(run_kwargs["envelope"])

    # Memory: the trusted audit + the block event — never the model output.
    ((_, _, writeback),) = gate_seams.persisted
    assert [e.kind for e in writeback.events] == [EventKind.TOOL_CALL, EventKind.ERROR]
    assert "MUST-NOT-PERSIST" not in str([e.model_dump() for e in writeback.events])

    retry = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers)
    assert retry.status_code == 422
    assert captured["posts"] == 1  # the violating run was not re-executed


def test_invoke_success_persists_writeback_and_completes_ledger(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cognition.models import EventKind

    captured: dict = {}
    event = {
        "id": "e1",
        "agent_id": "blogging.cog",
        "kind": "observation",
        "content": "learned a thing",
        "occurred_at": "2026-06-01T12:00:00+00:00",
        "source_run_id": "k1",
        "source_seq": 0,
    }
    _install_capturing_upstream(
        monkeypatch,
        captured,
        response_json={"output": {"ok": True}, "memory_events": [event, {"junk": 1}], "tool_audit": []},
    )
    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 200
    ((_, srid, writeback),) = gate_seams.persisted
    assert srid == "k1"
    assert [e.kind for e in writeback.events] == [EventKind.OBSERVATION]  # junk dropped
    assert gate_seams.ledger.rows[("blogging.cog", "k1")]["status"] == "completed"


def test_invoke_envelope_recap_413_when_cognition_overflows_cap(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A near-cap request that fits on its own but overflows once the cognition
    digest is folded in is rejected 413 at the proxy, never reaching the shim."""
    from agent_cognition.models import CognitionContext

    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)
    monkeypatch.setenv("AGENT_INVOKE_MAX_PAYLOAD_BYTES", "1024")
    gate_seams.context = CognitionContext(rules=[], memory_digest="D" * 900)

    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": "x" * 600})
    assert resp.status_code == 413
    assert "cognition" in resp.json()["detail"]
    assert captured["posts"] == 0


def test_invoke_warming_sandbox_returns_202_without_claiming(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warming 202 must precede the gate: claiming a leased run and then
    telling the caller to retry shortly would make every warm-up poll 409
    against its own claim until the lease expired."""
    _install_warming_sandbox(monkeypatch)
    headers = {"Idempotency-Key": "k1"}
    for _ in range(3):  # the UI polls; every poll must stay 202, never 409
        resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1}, headers=headers)
        assert resp.status_code == 202
    assert gate_seams.ledger.claims == 0
    assert gate_seams.ledger.rows == {}


def test_invoke_storage_outage_degrades_unless_side_effecting(
    cog_client: TestClient, gate_seams, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_cognition import invoke_gate as gate_mod
    from agent_cognition.memory.store import AgentCognitionStorageUnavailable

    def _boom(*a, **k):
        raise AgentCognitionStorageUnavailable("pg down")

    monkeypatch.setattr(gate_mod, "claim_run", _boom)

    captured: dict = {}
    _install_capturing_upstream(monkeypatch, captured)
    resp = cog_client.post("/api/agents/blogging.cog/invoke", json={"q": 1})
    assert resp.status_code == 200  # plain cognition agent degrades to unledgered

    resp = cog_client.post("/api/agents/blogging.sideeffect/invoke", json={"q": 1}, headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 503  # run-once not guaranteeable → fail closed
