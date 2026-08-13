"""Integration tests for the shim's cognition envelope + shim-driven tool loop.

Verifies the marker-gated unwrap, the cognition side channel, and that the shim
(not agent code) drives a returned ``ToolLoopPlan`` so the trusted ``tool_audit``
is produced in the shim's frame — agent code never holds the audit object.
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
from shared.agent_invoke import mount_invoke_shim  # noqa: E402


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

    class ReflectAgent:
        """A normal (non-plan) agent: reflects its input and the cognition block."""

        def run(self, body):
            import agent_cognition.tools.channel as ch

            return {"received": body, "cognition_seen": ch.get_cognition_context()}

    class PlanAgent:
        """Returns a ToolLoopPlan — the shim drives the loop and owns the audit."""

        def run(self, body):
            from agent_cognition.tools.runner import ToolLoopPlan

            return ToolLoopPlan(
                llm=_ScriptedLLM(),
                system_prompt="sys",
                user_prompt="go",
                toolset=_probe_toolset(),
            )

    class RaisingAgent:
        def run(self, body):
            raise RuntimeError("user-space failure")

    class _FailingLLM:
        """Calls `probe` once (a real side effect), then the loop errors out."""

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
            raise RuntimeError("loop failed on round 2")

    class FailingPlanAgent:
        """A plan whose loop fails mid-flight after a brokered call."""

        def run(self, body):
            from agent_cognition.tools.runner import ToolLoopPlan

            return ToolLoopPlan(
                llm=_FailingLLM(),
                system_prompt="sys",
                user_prompt="go",
                toolset=_probe_toolset(),
            )

    class _SlowLLM:
        """Completes a `probe` call (recorded), then blocks past the timeout."""

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
            import time as _t

            _t.sleep(5)  # the loop hangs here; the shim's wait_for fires first
            return {"done": True}

    class SlowPlanAgent:
        """A plan whose first tool call completes, then the loop blocks past timeout."""

        def run(self, body):
            from agent_cognition.tools.runner import ToolLoopPlan

            return ToolLoopPlan(
                llm=_SlowLLM(),
                system_prompt="sys",
                user_prompt="go",
                toolset=_probe_toolset(),
            )

    mod.ReflectAgent = ReflectAgent
    mod.PlanAgent = PlanAgent
    mod.RaisingAgent = RaisingAgent
    mod.FailingPlanAgent = FailingPlanAgent
    mod.SlowPlanAgent = SlowPlanAgent
    sys.modules["_cog_shim_test"] = mod

    for fname, agent_id, entry in (
        ("reflect.yaml", "blogging.reflect", "ReflectAgent"),
        ("plan.yaml", "blogging.plan", "PlanAgent"),
        ("raises.yaml", "blogging.raises", "RaisingAgent"),
        ("plan_fail.yaml", "blogging.plan_fail", "FailingPlanAgent"),
        ("plan_slow.yaml", "blogging.plan_slow", "SlowPlanAgent"),
    ):
        _write_manifest(
            tmp_path,
            fname,
            f"""
            schema_version: 1
            id: {agent_id}
            team: blogging
            name: {entry}
            summary: cognition test agent
            source:
              entrypoint: _cog_shim_test:{entry}
            """,
        )

    from agent_platform import registry
    from agent_platform.registry import loader

    if hasattr(loader.get_registry, "cache_clear"):
        loader.get_registry.cache_clear()
    rebuilt = loader.AgentRegistry.load(tmp_path)
    original_loader = loader.get_registry
    original_pkg = registry.get_registry
    loader.get_registry = lambda: rebuilt  # type: ignore[assignment]
    registry.get_registry = lambda: rebuilt  # type: ignore[assignment]

    app = FastAPI()
    mount_invoke_shim(app)
    try:
        yield TestClient(app)
    finally:
        loader.get_registry = original_loader  # type: ignore[assignment]
        registry.get_registry = original_pkg  # type: ignore[assignment]
        if hasattr(loader.get_registry, "cache_clear"):
            loader.get_registry.cache_clear()
        sys.modules.pop("_cog_shim_test", None)


def test_marked_envelope_delivers_input_only_and_side_channel(client: TestClient) -> None:
    resp = client.post(
        "/_agents/blogging.reflect/invoke",
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
    # A non-tool agent produces no audit, and there's no way for it to inject one.
    assert body["tool_audit"] == []


def test_unmarked_body_with_input_key_passes_through(client: TestClient) -> None:
    # A real agent whose own schema has a top-level `input` must not be unwrapped.
    payload = {"input": {"user": "data"}, "other": 7}
    resp = client.post("/_agents/blogging.reflect/invoke", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["output"]["received"] == payload  # whole body forwarded verbatim
    assert body["output"]["cognition_seen"] is None
    assert body["tool_audit"] == []


def test_shim_drives_tool_loop_plan_and_returns_trusted_audit(client: TestClient) -> None:
    resp = client.post(
        "/_agents/blogging.plan/invoke",
        json={ENVELOPE_MARKER: 1, "input": {"q": "hi"}, "cognition": {"rules": []}},
    )
    assert resp.status_code == 200
    body = resp.json()
    # The output is the loop's final result, produced by the shim-driven loop.
    assert body["output"] == {"done": True}
    # The trusted audit — produced in the shim's frame — carries the brokered call.
    assert len(body["tool_audit"]) == 1
    assert body["tool_audit"][0]["tool_id"] == "probe"
    assert body["tool_audit"][0]["function"] == "probe"
    assert body["tool_audit"][0]["ok"] is True
    # The episodic memory events are returned too (for the proxy to persist).
    assert body["memory_events"]
    assert any(ev["content"] == "probe" for ev in body["memory_events"])


def test_failing_tool_loop_returns_422_with_partial_audit(client: TestClient) -> None:
    # The loop fails after a real side effect: 422, but the trusted partial audit
    # (the brokered probe call) and its memory events must still come back.
    resp = client.post(
        "/_agents/blogging.plan_fail/invoke",
        json={ENVELOPE_MARKER: 1, "input": {}, "cognition": {"rules": []}},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "loop failed" in detail["error"]
    assert detail["tool_audit"] and detail["tool_audit"][0]["tool_id"] == "probe"
    assert detail["memory_events"]


def test_timeout_preserves_partial_audit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The loop's first tool call completes, then it blocks; the invoke times out
    # (504) but the trusted audit of the completed call must still come back.
    monkeypatch.setenv("AGENT_EXEC_TIMEOUT_S", "0.3")
    resp = client.post(
        "/_agents/blogging.plan_slow/invoke",
        json={ENVELOPE_MARKER: 1, "input": {}, "cognition": {"rules": []}},
    )
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert detail["timeout_hit"] is True
    assert detail["tool_audit"] and detail["tool_audit"][0]["tool_id"] == "probe"


def test_malformed_envelope_returns_400(client: TestClient) -> None:
    resp = client.post(
        "/_agents/blogging.reflect/invoke",
        json={ENVELOPE_MARKER: 1, "input": {}, "cognition": {}, "smuggled": "x"},
    )
    assert resp.status_code == 400
    assert "Malformed cognition envelope" in resp.json()["detail"]


def test_agent_exception_returns_422(client: TestClient) -> None:
    resp = client.post(
        "/_agents/blogging.raises/invoke",
        json={ENVELOPE_MARKER: 1, "input": {}, "cognition": {}},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"].startswith("RuntimeError:")
    assert detail["tool_audit"] == []
