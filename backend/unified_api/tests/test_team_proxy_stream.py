"""Tests for SSE streaming behaviour in unified_api.team_proxy.proxy_request.

Covers the fix for ERR_INCOMPLETE_CHUNKED_ENCODING on
``/api/deepthought/deepthought/ask/stream``:

1. On upstream HTTP errors mid-stream, the proxy emits a canonical SSE
   terminator (``event: error`` + ``event: done``) before closing.
2. Streaming-intent requests (path ending in ``/stream``) send a per-request
   timeout with ``read=None`` so sparse SSE streams are not cut by the
   client's default read deadline.
3. Non-streaming requests keep the default timeout — no SSE terminator bytes
   leak into their bodies.
4. The upstream response is always closed (``aclose`` called) on both success
   and error paths.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

_backend = Path(__file__).resolve().parent.parent.parent
if str(_backend) not in sys.path:
    sys.path.insert(0, str(_backend))
_agents = _backend / "agents"
if str(_agents) not in sys.path:
    sys.path.insert(0, str(_agents))

from unified_api import team_proxy
from unified_api.team_proxy import proxy_request


@pytest.fixture(autouse=True)
def _reset_proxy_state():
    """Reset the per-team client cache and circuit breaker between tests."""
    team_proxy._team_clients.clear()
    team_proxy.circuit_breaker._circuits.clear()  # type: ignore[attr-defined]
    yield
    team_proxy._team_clients.clear()


def _install_mock_client(team_key: str, transport: httpx.MockTransport) -> httpx.AsyncClient:
    """Inject a mock-backed AsyncClient into team_proxy's cache."""
    client = httpx.AsyncClient(transport=transport, base_url="http://upstream")
    team_proxy._team_clients[team_key] = client
    return client


def _build_app(upstream_base_url: str, team_key: str, *, timeout: float = 60.0) -> FastAPI:
    """Minimal FastAPI app whose routes delegate to proxy_request."""
    app = FastAPI()

    @app.api_route("/api/t/{path:path}", methods=["GET", "POST"])
    async def _proxy(request: Request, path: str):
        return await proxy_request(request, upstream_base_url, path, team_key=team_key, timeout=timeout)

    return app


# ---------------------------------------------------------------------------
# 1 + 2: terminator frame on upstream read timeout / remote disconnect
# ---------------------------------------------------------------------------


class _FlakyStream(httpx.AsyncByteStream):
    """An async byte stream that yields one valid SSE chunk then raises `exc`."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b'event: agent_event\ndata: {"msg":"hi"}\n\n'
        raise self._exc

    async def aclose(self) -> None:  # pragma: no cover - required by protocol
        return None


def _make_sse_transport(exc: Exception) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_FlakyStream(exc),
        )

    return httpx.MockTransport(handler)


def test_stream_yields_terminator_on_upstream_read_timeout():
    transport = _make_sse_transport(httpx.ReadTimeout("boom", request=None))
    _install_mock_client("deepthought", transport)
    app = _build_app("http://upstream", "deepthought")

    with TestClient(app) as client, client.stream("POST", "/api/t/deepthought/ask/stream", json={}) as resp:
        body = b"".join(resp.iter_bytes())

    text = body.decode("utf-8")
    assert "event: agent_event" in text
    assert "event: error" in text
    assert "upstream disconnected" in text
    assert text.rstrip().endswith("event: done\ndata: {}")


def test_stream_yields_terminator_on_remote_disconnect():
    transport = _make_sse_transport(httpx.RemoteProtocolError("disconnected", request=None))
    _install_mock_client("deepthought", transport)
    app = _build_app("http://upstream", "deepthought")

    with TestClient(app) as client, client.stream("POST", "/api/t/deepthought/ask/stream", json={}) as resp:
        body = b"".join(resp.iter_bytes())

    text = body.decode("utf-8")
    assert "event: error" in text
    assert "RemoteProtocolError" in text
    assert text.rstrip().endswith("event: done\ndata: {}")


# ---------------------------------------------------------------------------
# 3: per-request read=None timeout on streaming paths
# ---------------------------------------------------------------------------


def test_stream_path_uses_read_none_timeout():
    """Requests whose path ends in /stream carry a per-request timeout with read=None."""
    captured: dict = {}

    class _OneShotStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"event: agent_event\ndata: {}\n\nevent: done\ndata: {}\n\n"

        async def aclose(self) -> None:  # pragma: no cover
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_OneShotStream(),
        )

    _install_mock_client("deepthought", httpx.MockTransport(handler))
    app = _build_app("http://upstream", "deepthought")

    with TestClient(app) as client, client.stream("POST", "/api/t/deepthought/ask/stream", json={}) as resp:
        b"".join(resp.iter_bytes())

    # httpx stores per-request timeout under request.extensions["timeout"] as a dict
    # with keys connect/read/write/pool.
    to = captured["timeout"]
    assert isinstance(to, dict), f"expected dict timeout, got {to!r}"
    assert to.get("read") is None
    assert to.get("connect") == 10.0


def test_non_stream_path_does_not_override_timeout():
    """Non-streaming paths don't inject a per-request timeout — client default applies."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json={"ok": True})

    _install_mock_client("deepthought", httpx.MockTransport(handler))
    app = _build_app("http://upstream", "deepthought")

    with TestClient(app) as client:
        resp = client.post("/api/t/deepthought/ask", json={})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # No per-request timeout override — extension absent or defaults to client-level values.
    to = captured["timeout"]
    if isinstance(to, dict):
        # If httpx populated defaults, read should NOT be None (i.e. the client default applies).
        assert to.get("read") is not None


# ---------------------------------------------------------------------------
# 4: resp.aclose always called
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_team_client: lazy creation + caching
# ---------------------------------------------------------------------------


def test_get_team_client_creates_and_caches_per_team():
    client_a = team_proxy.get_team_client("team-a", timeout=5.0)
    client_b = team_proxy.get_team_client("team-a")
    assert client_a is client_b
    other = team_proxy.get_team_client("team-b")
    assert other is not client_a


# ---------------------------------------------------------------------------
# Circuit breaker open -> 503 without reaching upstream
# ---------------------------------------------------------------------------


def test_circuit_breaker_open_short_circuits_to_503():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be called
        raise AssertionError("upstream should not be reached when breaker is open")

    _install_mock_client("flaky", httpx.MockTransport(handler))
    app = _build_app("http://upstream", "flaky")

    # Trip the breaker directly (5 consecutive failures by default).
    for _ in range(5):
        team_proxy.circuit_breaker.record_failure("flaky")
    assert team_proxy.circuit_breaker.is_open("flaky") is True

    with TestClient(app) as client:
        resp = client.get("/api/t/anything")

    assert resp.status_code == 503
    assert b"circuit breaker is open" in resp.content


# ---------------------------------------------------------------------------
# Query string forwarding
# ---------------------------------------------------------------------------


def test_query_string_is_forwarded():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    _install_mock_client("qs-team", httpx.MockTransport(handler))
    app = _build_app("http://upstream", "qs-team")

    with TestClient(app) as client:
        resp = client.get("/api/t/things", params={"foo": "bar"})

    assert resp.status_code == 200
    assert "foo=bar" in captured["url"]


# ---------------------------------------------------------------------------
# Upstream connection failure -> 502, and 5xx responses record a breaker failure
# ---------------------------------------------------------------------------


def test_upstream_connection_error_returns_502():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_mock_client("bad-upstream", httpx.MockTransport(handler))
    app = _build_app("http://upstream", "bad-upstream")

    with TestClient(app) as client:
        resp = client.get("/api/t/anything")

    assert resp.status_code == 502
    assert b"Bad Gateway" in resp.content
    assert team_proxy.circuit_breaker.is_open("bad-upstream") is False  # only 1 failure, threshold is 5


def test_upstream_5xx_records_circuit_breaker_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    _install_mock_client("server-error-team", httpx.MockTransport(handler))
    app = _build_app("http://upstream", "server-error-team")

    with TestClient(app) as client:
        for _ in range(5):
            resp = client.get("/api/t/anything")
            assert resp.status_code == 500

    assert team_proxy.circuit_breaker.is_open("server-error-team") is True


def test_resp_aclose_called_on_error_path():
    """When the upstream stream raises, the response is still closed cleanly."""
    closed = {"count": 0}

    class _FlakyCountingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"event: a\ndata: 1\n\n"
            raise httpx.ReadTimeout("boom", request=None)

        async def aclose(self) -> None:
            closed["count"] += 1

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_FlakyCountingStream(),
        )

    _install_mock_client("deepthought", httpx.MockTransport(handler))
    app = _build_app("http://upstream", "deepthought")

    with TestClient(app) as client, client.stream("POST", "/api/t/deepthought/ask/stream", json={}) as resp:
        b"".join(resp.iter_bytes())

    assert closed["count"] >= 1
