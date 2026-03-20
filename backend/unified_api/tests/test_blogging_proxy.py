"""Tests for blogging remote proxy wiring."""

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_try_mount_blogging_registers_proxy_when_remote_url_set() -> None:
    from unified_api import main as unified_main

    app = FastAPI()
    with patch.object(unified_main, "BLOGGING_REMOTE_URL", "http://blogging-api:8081"):
        mounted = unified_main._try_mount_blogging(app)

    assert mounted is True
    paths = {route.path for route in app.routes}
    assert "/api/blogging" in paths
    assert "/api/blogging/{subpath:path}" in paths


def test_blogging_proxy_forwards_success_response() -> None:
    from unified_api import main as unified_main

    app = FastAPI()
    unified_main._register_blogging_proxy_routes(app, "http://blogging-api:8081")

    mock_response = AsyncMock()
    mock_response.content = b'{"status":"ok"}'
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "application/json"}

    with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=mock_response)):
        with TestClient(app) as client:
            resp = client.get("/api/blogging/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_blogging_proxy_returns_503_when_upstream_unavailable() -> None:
    import httpx
    from unified_api import main as unified_main

    app = FastAPI()
    unified_main._register_blogging_proxy_routes(app, "http://blogging-api:8081")

    with patch(
        "httpx.AsyncClient.request",
        new=AsyncMock(side_effect=httpx.RequestError("boom")),
    ):
        with TestClient(app) as client:
            resp = client.get("/api/blogging/health")

    assert resp.status_code == 503
    assert "Blogging service unavailable" in resp.text
