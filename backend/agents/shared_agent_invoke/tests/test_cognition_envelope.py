"""Integration tests for the shim's cognition envelope handling (Step 7).

Verifies the marker-gated unwrap, the side channel, the trusted tool-audit, and
that an unmarked body — even one with its own top-level ``input`` key — passes
through untouched.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from textwrap import dedent

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_backend = Path(__file__).resolve().parent.parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from agent_cognition.tools.envelope import ENVELOPE_MARKER  # noqa: E402
from shared_agent_invoke import mount_invoke_shim  # noqa: E402


def _write_manifest(tmp_path: Path, filename: str, body: str) -> None:
    d = tmp_path / "blogging" / "agent_console" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path):
    mod = types.ModuleType("_cog_shim_test")

    class CognitionAgent:
        """Records a trusted tool-audit entry and reflects what it received."""

        def run(self, body):
            import agent_cognition.tools.channel as ch

            ch.record_tool_audit({"tool_id": "probe", "args": {}, "ok": True})
            return {"received": body, "cognition_seen": ch.get_cognition_context()}

    class RaisingCognitionAgent:
        """Records an audit entry, then fails (writeback would be dropped)."""

        def run(self, body):
            import agent_cognition.tools.channel as ch

            ch.record_tool_audit({"tool_id": "probe", "args": {}, "ok": True})
            raise RuntimeError("boom after a tool call")

    mod.CognitionAgent = CognitionAgent
    mod.RaisingCognitionAgent = RaisingCognitionAgent
    sys.modules["_cog_shim_test"] = mod

    _write_manifest(
        tmp_path,
        "cog.yaml",
        """
        schema_version: 1
        id: blogging.cog
        team: blogging
        name: Cog
        summary: cognition agent
        source:
          entrypoint: _cog_shim_test:CognitionAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "cog_raises.yaml",
        """
        schema_version: 1
        id: blogging.cog_raises
        team: blogging
        name: Cog Raises
        summary: cognition agent that raises
        source:
          entrypoint: _cog_shim_test:RaisingCognitionAgent
        """,
    )

    import agent_registry
    from agent_registry import loader

    if hasattr(loader.get_registry, "cache_clear"):
        loader.get_registry.cache_clear()
    rebuilt = loader.AgentRegistry.load(tmp_path)
    original_loader = loader.get_registry
    original_pkg = agent_registry.get_registry
    loader.get_registry = lambda: rebuilt  # type: ignore[assignment]
    agent_registry.get_registry = lambda: rebuilt  # type: ignore[assignment]

    app = FastAPI()
    mount_invoke_shim(app)
    try:
        yield TestClient(app)
    finally:
        loader.get_registry = original_loader  # type: ignore[assignment]
        agent_registry.get_registry = original_pkg  # type: ignore[assignment]
        if hasattr(loader.get_registry, "cache_clear"):
            loader.get_registry.cache_clear()
        sys.modules.pop("_cog_shim_test", None)


def test_marked_envelope_delivers_input_only_and_side_channel(client: TestClient) -> None:
    resp = client.post(
        "/_agents/blogging.cog/invoke",
        json={
            ENVELOPE_MARKER: 1,
            "input": {"q": "hi"},
            "cognition": {"rules": [], "memory_digest": "remember this"},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    # Entrypoint received ONLY its declared input — never the cognition block.
    assert body["output"]["received"] == {"q": "hi"}
    # Cognition rode the side channel.
    assert body["output"]["cognition_seen"] == {"rules": [], "memory_digest": "remember this"}
    # The broker's trusted audit came back out-of-band.
    assert body["tool_audit"] == [{"tool_id": "probe", "args": {}, "ok": True}]


def test_unmarked_body_with_input_key_passes_through(client: TestClient) -> None:
    # A real agent whose own schema has a top-level `input` must not be unwrapped.
    payload = {"input": {"user": "data"}, "other": 7}
    resp = client.post("/_agents/blogging.cog/invoke", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["output"]["received"] == payload  # whole body forwarded verbatim
    assert body["output"]["cognition_seen"] is None
    # Audit sink is still opened (so sandbox-local tools are audited even without
    # an injected cognition block).
    assert body["tool_audit"] == [{"tool_id": "probe", "args": {}, "ok": True}]


def test_malformed_envelope_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/_agents/blogging.cog/invoke",
        json={ENVELOPE_MARKER: 1, "input": {}, "cognition": {}, "smuggled": "x"},
    )
    assert resp.status_code == 400
    assert "Malformed cognition envelope" in resp.json()["detail"]


def test_audit_survives_a_dropped_writeback(client: TestClient) -> None:
    # The agent raises after a tool call: the 422 envelope must still carry the
    # trusted tool-audit (the blocked/failed run audit guarantee).
    resp = client.post(
        "/_agents/blogging.cog_raises/invoke",
        json={ENVELOPE_MARKER: 1, "input": {}, "cognition": {}},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"].startswith("RuntimeError:")
    assert detail["tool_audit"] == [{"tool_id": "probe", "args": {}, "ok": True}]
