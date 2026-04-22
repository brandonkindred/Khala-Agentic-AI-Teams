"""Integration tests for the mount_invoke_shim FastAPI route.

Verifies the three distinct failure modes return the right HTTP status:
  - AgentNotRunnable (bad entrypoint, missing symbol) → 500
  - user-space exception raised by the agent                → 422
  - requires-live-integration tag on the manifest           → 409
and the happy path returns 200 with the envelope shape.
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

from shared_agent_invoke import mount_invoke_shim  # noqa: E402


def _write_manifest(tmp_path: Path, filename: str, body: str) -> None:
    d = tmp_path / "blogging" / "agent_console" / "manifests"
    d.mkdir(parents=True, exist_ok=True)
    (d / filename).write_text(dedent(body).lstrip(), encoding="utf-8")


@pytest.fixture()
def client(tmp_path: Path):
    # Install a runnable stub agent + a broken-entrypoint manifest in the registry.
    runnable_mod = types.ModuleType("_shim_test_runnable")

    class GoodAgent:
        def run(self, body):
            return {"echoed": body}

    class RaisingAgent:
        def run(self, body):
            raise RuntimeError("user-space failure")

    class SleepAgent:
        async def run(self, body):
            import asyncio

            await asyncio.sleep(10)
            return {"never": "returned"}

    class BigAgent:
        def run(self, body):
            # Deliberately larger than the default 1 MiB output cap so tests
            # that tune AGENT_INVOKE_MAX_OUTPUT_BYTES downward trip truncation.
            return {"blob": "x" * (2 * 1024 * 1024)}

    class SentinelAgent:
        def run(self, body):
            raise AssertionError("SentinelAgent must never be invoked")

    runnable_mod.GoodAgent = GoodAgent
    runnable_mod.RaisingAgent = RaisingAgent
    runnable_mod.SleepAgent = SleepAgent
    runnable_mod.BigAgent = BigAgent
    runnable_mod.SentinelAgent = SentinelAgent
    sys.modules["_shim_test_runnable"] = runnable_mod

    _write_manifest(
        tmp_path,
        "good.yaml",
        """
        schema_version: 1
        id: blogging.good
        team: blogging
        name: Good
        summary: runs fine
        source:
          entrypoint: _shim_test_runnable:GoodAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "raises.yaml",
        """
        schema_version: 1
        id: blogging.raises
        team: blogging
        name: Raises
        summary: raises inside run
        source:
          entrypoint: _shim_test_runnable:RaisingAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "broken.yaml",
        """
        schema_version: 1
        id: blogging.broken
        team: blogging
        name: Broken
        summary: missing symbol
        source:
          entrypoint: _shim_test_runnable:NoSuchSymbol
        """,
    )
    _write_manifest(
        tmp_path,
        "live.yaml",
        """
        schema_version: 1
        id: blogging.live
        team: blogging
        name: Live
        summary: requires live integration
        tags: [requires-live-integration]
        source:
          entrypoint: _shim_test_runnable:GoodAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "sleeper.yaml",
        """
        schema_version: 1
        id: blogging.sleeper
        team: blogging
        name: Sleeper
        summary: never returns in time
        source:
          entrypoint: _shim_test_runnable:SleepAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "sleeper_override.yaml",
        """
        schema_version: 1
        id: blogging.sleeper_override
        team: blogging
        name: Sleeper with per-manifest override
        summary: sleeps long, but manifest caps it low
        invoke:
          kind: http
          timeout_seconds: 0.2
        source:
          entrypoint: _shim_test_runnable:SleepAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "big.yaml",
        """
        schema_version: 1
        id: blogging.big
        team: blogging
        name: Big
        summary: returns oversized output
        source:
          entrypoint: _shim_test_runnable:BigAgent
        """,
    )
    _write_manifest(
        tmp_path,
        "sentinel.yaml",
        """
        schema_version: 1
        id: blogging.sentinel
        team: blogging
        name: Sentinel
        summary: must not run
        source:
          entrypoint: _shim_test_runnable:SentinelAgent
        """,
    )

    # Rebuild and patch the registry singleton. The shim re-imports
    # `from agent_registry import get_registry` each call, so we must patch
    # the package-level binding, not just the loader module's.
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
        sys.modules.pop("_shim_test_runnable", None)


def test_happy_path_returns_200_envelope(client: TestClient) -> None:
    resp = client.post("/_agents/blogging.good/invoke", json={"x": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["output"] == {"echoed": {"x": 1}}
    assert body["error"] is None
    assert "trace_id" in body


def test_user_space_exception_returns_422_with_envelope(client: TestClient) -> None:
    resp = client.post("/_agents/blogging.raises/invoke", json={})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"].startswith("RuntimeError:")
    assert detail["output"] is None


def test_dispatch_failure_returns_500_not_200(client: TestClient) -> None:
    # Regression for P2 review finding: AgentNotRunnableError (missing symbol,
    # bad entrypoint) must NOT return 200 OK or clients that rely on status
    # codes will treat an infra failure as a successful invocation.
    resp = client.post("/_agents/blogging.broken/invoke", json={})
    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "AgentNotRunnable" in detail["error"]
    assert detail["output"] is None


def test_requires_live_integration_returns_409(client: TestClient) -> None:
    resp = client.post("/_agents/blogging.live/invoke", json={})
    assert resp.status_code == 409


def test_unknown_agent_returns_404(client: TestClient) -> None:
    resp = client.post("/_agents/does.not.exist/invoke", json={})
    assert resp.status_code == 404


def test_oversized_body_returns_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # Cap to 1 KiB so the test doesn't allocate megabytes.
    monkeypatch.setenv("AGENT_INVOKE_MAX_PAYLOAD_BYTES", "1024")
    payload = "x" * 4096  # 4 KiB — well over the cap
    resp = client.post(
        "/_agents/blogging.sentinel/invoke",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 413
    # Sentinel agent raises AssertionError if called — absence of 500 proves
    # the cap short-circuits before dispatch.


def test_timeout_returns_504_with_envelope(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_EXEC_TIMEOUT_S", "0.1")
    resp = client.post("/_agents/blogging.sleeper/invoke", json={})
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert detail["timeout_hit"] is True
    assert detail["error"].startswith("AgentExecutionTimeout:")
    assert "trace_id" in detail


def test_oversized_output_sets_truncated_flag(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Cap at 10 KiB — BigAgent returns ~2 MiB.
    monkeypatch.setenv("AGENT_INVOKE_MAX_OUTPUT_BYTES", "10240")
    resp = client.post("/_agents/blogging.big/invoke", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["truncated"] is True
    assert body["error"] is None
    assert body["output"]["__truncated__"] is True
    assert body["output"]["original_size"] > 10240
    assert len(body["output"]["preview"]) == 10240


def test_per_manifest_timeout_overrides_env_default(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Env default is very generous; manifest override pins it to 0.2s.
    monkeypatch.setenv("AGENT_EXEC_TIMEOUT_S", "60")
    resp = client.post("/_agents/blogging.sleeper_override/invoke", json={})
    assert resp.status_code == 504
    detail = resp.json()["detail"]
    assert detail["timeout_hit"] is True
    # Timeout message reflects the manifest's 0.2s override, not the env 60s.
    assert "0.2s" in detail["error"]
