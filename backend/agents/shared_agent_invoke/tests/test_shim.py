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

    runnable_mod.GoodAgent = GoodAgent
    runnable_mod.RaisingAgent = RaisingAgent
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
    mount_invoke_shim(app, team_key="blogging")

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


def test_wrong_team_returns_404(client: TestClient) -> None:
    # Mount the shim on a different team and hit a blogging agent.
    from agent_registry import loader
    from shared_agent_invoke import mount_invoke_shim as _mount

    app = FastAPI()
    _mount(app, team_key="branding")
    c = TestClient(app)
    # Registry still has blogging.good from the fixture.
    resp = c.post("/_agents/blogging.good/invoke", json={})
    assert resp.status_code == 404
    assert hasattr(loader, "get_registry")
