"""
ASGI middleware that runs the security agent on requests to team API prefixes.

Collects the request body, runs the security scan, and either returns 403 with
security_findings or forwards the request with the body replayed to the downstream app.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from unified_api.config import SECURITY_GATEWAY_ENABLED, TEAM_CONFIGS
from unified_api.security import scan

logger = logging.getLogger("unified_api.security_gateway")

# Exact detail message from plan for 403 response
SECURITY_ERROR_DETAIL = (
    "Request did not pass security check. The request was found to contain content "
    "that may be malicious, destructive, or harmful to the system."
)


def _get_team_prefixes() -> set[str]:
    """Build set of team API prefixes from TEAM_CONFIGS."""
    return {config.prefix for config in TEAM_CONFIGS.values()}


def _is_team_path(path: str) -> bool:
    """True if path is under any team API prefix (e.g. /api/blogging/...)."""
    prefixes = _get_team_prefixes()
    return any(path.startswith(prefix) for prefix in prefixes)


class SecurityGatewayMiddleware:
    """
    ASGI middleware: for http requests to team prefixes, collect body, run security
    scan, then either return 403 with security_findings or forward with replayed body.
    When SECURITY_GATEWAY_ENABLED is False, forwards without scanning.
    """

    def __init__(self, app: Callable[..., Any]) -> None:
        self.app = app
        self._team_prefixes = _get_team_prefixes()

    async def __call__(
        self,
        scope: dict,
        receive: Callable[[], Any],
        send: Callable[[Any], Any],
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if not _is_team_path(path):
            await self.app(scope, receive, send)
            return

        # Collect body by wrapping receive
        messages: list[dict] = []
        body_chunks: list[bytes] = []

        async def collecting_receive() -> dict:
            message = await receive()
            messages.append(message)
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if body:
                    body_chunks.append(body)
            return message

        # Consume the request stream to get full body
        while True:
            message = await collecting_receive()
            if message.get("type") == "http.disconnect":
                break
            if message.get("type") == "http.request" and not message.get("more_body", False):
                break

        body_bytes = b"".join(body_chunks)

        # When disabled, forward with replayed body
        if not SECURITY_GATEWAY_ENABLED:
            await self._forward_with_replay(scope, messages, send)
            return

        # Run security agent
        method = scope.get("method", "").encode() or b""
        query_string = scope.get("query_string") or b""
        headers = scope.get("headers") or []
        passed, findings = scan(
            method.decode("utf-8", errors="replace"),
            path,
            query_string,
            list(headers),
            body_bytes,
        )

        if not passed and findings:
            await self._send_403(send, findings)
            return

        await self._forward_with_replay(scope, messages, send)

    async def _send_403(self, send: Callable[[Any], Any], security_findings: list[str]) -> None:
        """Send 403 Forbidden with JSON body (detail + security_findings)."""
        body = json.dumps(
            {"detail": SECURITY_ERROR_DETAIL, "security_findings": security_findings},
            ensure_ascii=False,
        ).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body, "more_body": False})

    async def _forward_with_replay(
        self,
        scope: dict,
        messages: list[dict],
        send: Callable[[Any], Any],
    ) -> None:
        """Call the app with a receive that replays the collected messages."""
        it = iter(messages)

        async def replay_receive() -> dict:
            try:
                return next(it)
            except StopIteration:
                return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)
