"""Reusable HTTP reverse proxy for extracted team microservices.

When a team is deployed as its own container, the unified API forwards
requests to the team's service URL using this module.  The proxy is
transparent: method, path, query string, headers, and body are forwarded
and the upstream response is returned verbatim.

Architecture improvements over the original single-client design:

- **Per-team connection pools**: Each team gets its own ``httpx.AsyncClient``
  with isolated connection limits, so a slow team cannot exhaust the
  connection pool for other teams.
- **Circuit breaker**: After consecutive failures, requests to a team are
  short-circuited with 503 instead of waiting for upstream timeout.
- **Per-team timeouts**: Each team has a configured ``timeout_seconds``
  (from ``TeamConfig``) rather than a blanket 300s for all teams.
- **Request correlation**: Generates ``X-Request-ID`` if not present and
  forwards it to upstream for distributed tracing.

Usage from a mount function::

    from unified_api.team_proxy import proxy_request, get_team_client

    @app.api_route("/api/my-team/{path:path}", methods=[...])
    async def _proxy(request: Request, path: str):
        return await proxy_request(request, "http://my-team:8090", path, team_key="my_team")
"""

from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from unified_api.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# Per-team async clients, created lazily on first use.
_team_clients: dict[str, httpx.AsyncClient] = {}

# Global circuit breaker instance (shared across all proxy routes).
circuit_breaker = CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)

# Headers that must not be forwarded between hops.
_HOP_BY_HOP_REQUEST = frozenset({"host", "connection", "transfer-encoding"})
_HOP_BY_HOP_RESPONSE = frozenset({"transfer-encoding", "connection", "content-encoding"})

# Default timeout for teams that don't specify one.
_DEFAULT_TIMEOUT = 60.0


def get_team_client(team_key: str, timeout: float | None = None) -> httpx.AsyncClient:
    """Return a per-team async HTTP client (created lazily, cached).

    Each team gets its own connection pool so a slow/unresponsive team
    cannot exhaust connections for other teams.
    """
    if team_key not in _team_clients:
        t = timeout or _DEFAULT_TIMEOUT
        _team_clients[team_key] = httpx.AsyncClient(
            timeout=httpx.Timeout(t, connect=10.0),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _team_clients[team_key]


async def proxy_request(
    request: Request,
    target_base_url: str,
    path: str,
    *,
    team_key: str = "",
    timeout: float | None = None,
) -> Response:
    """Forward a FastAPI *request* to *target_base_url*/*path* and return the response.

    Parameters
    ----------
    team_key:
        Used for per-team connection pool isolation, circuit breaker tracking,
        and log correlation. If empty, a shared default client is used.
    timeout:
        Per-team timeout in seconds (from ``TeamConfig.timeout_seconds``).
    """
    effective_key = team_key or "_default"

    # Circuit breaker check
    if circuit_breaker.is_open(effective_key):
        logger.warning("Circuit breaker OPEN for team %s — returning 503", effective_key)
        return Response(
            content=f"Service temporarily unavailable: team '{team_key}' circuit breaker is open",
            status_code=503,
            media_type="text/plain",
        )

    client = get_team_client(effective_key, timeout)

    url = f"{target_base_url.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # Forward headers, stripping hop-by-hop. Inject X-Request-ID for correlation.
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_REQUEST}
    if "x-request-id" not in headers:
        headers["x-request-id"] = str(uuid.uuid4())

    body = await request.body()

    try:
        req = client.build_request(method=request.method, url=url, headers=headers, content=body)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        circuit_breaker.record_failure(effective_key)
        logger.error("Proxy error: %s %s -> %s: %s", request.method, request.url.path, url, exc)
        return Response(
            content=f"Bad Gateway: upstream service unavailable ({type(exc).__name__})",
            status_code=502,
            media_type="text/plain",
        )

    # Record success for circuit breaker (upstream responded, even if with an error status)
    if resp.status_code < 500:
        circuit_breaker.record_success(effective_key)
    else:
        circuit_breaker.record_failure(effective_key)

    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP_RESPONSE}
    content_type = resp.headers.get("content-type", "")

    # Stream SSE responses through without buffering.
    if "text/event-stream" in content_type:

        async def _stream():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            except httpx.HTTPError as exc:
                logger.error(
                    "Proxy upstream disconnected mid-stream: %s %s -> %s: %s",
                    request.method, request.url.path, url, exc,
                )
            finally:
                await resp.aclose()

        return StreamingResponse(
            _stream(), status_code=resp.status_code, headers=resp_headers, media_type="text/event-stream"
        )

    # Non-streaming: read full body and return.
    await resp.aread()
    await resp.aclose()
    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)
