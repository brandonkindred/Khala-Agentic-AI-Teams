"""End-to-end regression test for the assistant first-request lazy-mount hook.

Proves the routing-priority fix actually works: without repositioning the
newly-mounted route ahead of the team's already-registered proxy catch-all
(`{prefix}/{path:path}`), Starlette's router would always match the
catch-all first (it was registered at lifespan startup, before any
assistant is ever mounted) and every assistant request would be silently
swallowed by the proxy handler instead of reaching the assistant sub-app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import unified_api.main as main


@pytest.fixture(autouse=True)
def _isolate_app_state():
    """This test calls lifespan step functions directly against the shared
    unified_api.main.app singleton (mutating its routes list and mount
    state), so it must not leak into other test modules that import the
    same app instance."""
    routes_snapshot = list(main.app.routes)
    mounted_snapshot = set(main._MOUNTED_ASSISTANTS)
    locks_snapshot = dict(main._ASSISTANT_MOUNT_LOCKS)
    registry_snapshot = dict(main._ASSISTANT_REGISTRY)
    registered_teams_snapshot = dict(main._registered_teams)
    yield
    main.app.routes[:] = routes_snapshot
    main._MOUNTED_ASSISTANTS.clear()
    main._MOUNTED_ASSISTANTS.update(mounted_snapshot)
    main._ASSISTANT_MOUNT_LOCKS.clear()
    main._ASSISTANT_MOUNT_LOCKS.update(locks_snapshot)
    main._ASSISTANT_REGISTRY.clear()
    main._ASSISTANT_REGISTRY.update(registry_snapshot)
    main._registered_teams.clear()
    main._registered_teams.update(registered_teams_snapshot)


def test_assistant_health_reaches_mounted_subapp_not_proxy_catchall(monkeypatch: pytest.MonkeyPatch) -> None:
    """A first request to a team's {prefix}/assistant/health must reach the
    mounted assistant sub-app, not the team's proxy catch-all — even though
    the catch-all was registered first."""
    monkeypatch.setenv("BLOGGING_SERVICE_URL", "http://fake-upstream.invalid")

    main._maybe_register_team_assistants()
    main._register_proxy_routes(main.app)
    assert "blogging" in main._ASSISTANT_REGISTRY, "blogging must have a registered assistant spec"

    client = TestClient(main.app)
    resp = client.get("/api/blogging/assistant/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert "blogging" in main._MOUNTED_ASSISTANTS


def test_unregistered_assistant_path_still_hits_proxy_catchall(monkeypatch: pytest.MonkeyPatch) -> None:
    """A path under a team with no registered assistant must still reach the
    proxy catch-all — the reorder fix must be scoped to the one mounted
    prefix, not global."""
    monkeypatch.setenv("BLOGGING_SERVICE_URL", "http://fake-upstream.invalid")

    async def fake_proxy_request(request, url, path, team_key, timeout):
        return {"proxied": True, "team_key": team_key, "path": path}

    monkeypatch.setattr("unified_api.team_proxy.proxy_request", fake_proxy_request)

    main._ASSISTANT_REGISTRY.clear()  # simulate assistants disabled entirely
    main._register_proxy_routes(main.app)

    client = TestClient(main.app)
    resp = client.get("/api/blogging/assistant/health")

    assert resp.status_code == 200
    assert resp.json() == {"proxied": True, "team_key": "blogging", "path": "assistant/health"}
