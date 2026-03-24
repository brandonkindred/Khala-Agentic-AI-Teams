"""Reusable HTTP reverse proxy for extracted team microservices.

When a team is deployed as its own container, the unified API forwards
requests to the team's service URL using this module.  The proxy is
transparent: method, path, query string, headers, and body are forwarded
and the upstream response is returned verbatim.

Usage from a mount function::

    from unified_api.team_proxy import proxy_request

    @app.api_route("/api/my-team/{path:path}", methods=[...])
    async def _proxy(request: Request, path: str):
        return await proxy_request(request, "http://my-team:8090", path)
"""

from __future__ import annotations

import logging

import httpx
from fastapi import Request, Response

logger = logging.getLogger(__name__)

# Module-level async client, created once on first use.
_client: httpx.AsyncClient | None = None

# Headers that must not be forwarded between hops.
_HOP_BY_HOP_REQUEST = frozenset({"host", "connection", "transfer-encoding"})
_HOP_BY_HOP_RESPONSE = frozenset({"transfer-encoding", "connection", "content-encoding"})


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=10.0),
            follow_redirects=False,
        )
    return _client


async def proxy_request(request: Request, target_base_url: str, path: str) -> Response:
    """Forward a FastAPI *request* to *target_base_url*/*path* and return the response."""
    client = _get_client()

    url = f"{target_base_url.rstrip('/')}/{path.lstrip('/')}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    # Forward headers, stripping hop-by-hop.
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP_REQUEST}

    body = await request.body()

    resp = await client.request(
        method=request.method,
        url=url,
        headers=headers,
        content=body,
    )

    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP_RESPONSE}

    return Response(content=resp.content, status_code=resp.status_code, headers=resp_headers)
