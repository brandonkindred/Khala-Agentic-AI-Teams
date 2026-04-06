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
from fastapi.responses import StreamingResponse

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

    try:
        req = client.build_request(method=request.method, url=url, headers=headers, content=body)
        resp = await client.send(req, stream=True)
    except httpx.HTTPError as exc:
        logger.error("Proxy error: %s %s -> %s: %s", request.method, request.url.path, url, exc)
        return Response(
            content=f"Bad Gateway: upstream service unavailable ({type(exc).__name__})",
            status_code=502,
            media_type="text/plain",
        )

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
