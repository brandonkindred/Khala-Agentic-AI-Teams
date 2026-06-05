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

    class _ScriptedLLM:
        """Calls `probe` once, then returns a final JSON object."""

        def __init__(self) -> None:
            self.n = 0

        def chat(self, messages, **_kwargs):
            self.n += 1
            if self.n == 1:
                return {
                    "__tool_calls__": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "probe", "arguments": {}},
                        }
                    ]
                }
            return {"done": True}

    def _probe_toolset():
        from agent_cognition.tools.binding import BoundTool, BoundToolset, ExecutionSite

        defn = {
            "type": "function",
            "function": {
                "name": "probe",
                "description": "probe",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": True},
            },
        }
        return BoundToolset(
            (
                BoundTool(
                    tool_id="probe",
                    site=ExecutionSite.SANDBOX_LOCAL,
                    definitions=(defn,),
                    handlers={"probe": lambda a: {"ok": True}},
                ),
            )
        )

    def _run_brokered_probe():
        from agent_cognition.tools.runner import run_tool_loop

        run_tool_loop(
            _ScriptedLLM(),
            agent_id="blogging.cog",
            source_run_id="r1",
            user_prompt="",
            system_prompt="",
            toolset=_probe_toolset(),
            enforced_rules=[],
        )

    class CognitionAgent:
        """Runs a brokered tool call (which feeds the audit) and reflects input."""

        def run(self, body):
            import agent_cognition.tools.channel as ch

            _run_brokered_probe()
            return {"received": body, "cognition_seen": ch.get_cognition_context()}

    class RaisingCognitionAgent:
        """Runs a brokered tool call, then fails (writeback would be dropped)."""

        def run(self, body):
            _run_brokered_probe()
            raise RuntimeError("boom after a tool call")

    class ForgingAgent:
        """Tries to forge an audit entry outside the broker path (must be a no-op)."""

        def run(self, body):
            import agent_cognition.tools.channel as ch

            # No public writer exists; the private path refuses writes outside a
            # broker recording window, so this must NOT pollute the audit.
            ch._record_brokered({"tool_id": "forged", "ok": True})
            return {"received": body}

    mod.CognitionAgent = CognitionAgent
    mod.RaisingCognitionAgent = RaisingCognitionAgent
    mod.ForgingAgent = ForgingAgent
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
    _write_manifest(
        tmp_path,
        "cog_forge.yaml",
        """
        schema_version: 1
        id: blogging.cog_forge
        team: blogging
        name: Cog Forge
        summary: cognition agent that tries to forge the audit
        source:
          entrypoint: _cog_shim_test:ForgingAgent
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
    # The broker's trusted audit came back out-of-band (one ToolCall dump).
    assert len(body["tool_audit"]) == 1
    assert body["tool_audit"][0]["tool_id"] == "probe"
    assert body["tool_audit"][0]["ok"] is True


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
    assert len(body["tool_audit"]) == 1
    assert body["tool_audit"][0]["tool_id"] == "probe"


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
    assert len(detail["tool_audit"]) == 1
    assert detail["tool_audit"][0]["tool_id"] == "probe"


def test_agent_cannot_forge_audit_outside_the_broker(client: TestClient) -> None:
    # An agent calling the private writer outside a broker recording window must
    # not be able to inject a forged entry into the trusted audit.
    resp = client.post(
        "/_agents/blogging.cog_forge/invoke",
        json={ENVELOPE_MARKER: 1, "input": {}, "cognition": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["tool_audit"] == []
