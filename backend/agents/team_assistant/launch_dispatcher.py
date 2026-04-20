"""In-process dispatcher that turns a :class:`LaunchSpec` + context into an
HTTP call against a team's real run endpoint.

Uses ``httpx.ASGITransport`` to re-enter the unified API within the same
process — identical to the pattern in
``unified_api.slack_events_handler._call_team_assistant``. This preserves
the normal request pipeline (security gateway, team proxy) without
requiring a network round-trip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from team_assistant.launch_spec import LaunchSpec

logger = logging.getLogger(__name__)

# Use the longest per-team proxy timeout (300s, see unified_api.config) so
# we never time out before the upstream does.
DEFAULT_DISPATCH_TIMEOUT_S = 300.0


@dataclass
class DispatchResult:
    status: int
    body: dict[str, Any]
    job_id: str | None


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {"_raw_text": resp.text[:2000]}
    if isinstance(data, dict):
        return data
    return {"_value": data}


async def dispatch(spec: LaunchSpec, context: dict[str, Any]) -> DispatchResult:
    """Build the request from ``context`` and dispatch it in-process."""
    # Import lazily to avoid circular imports — the unified API imports
    # team_assistant during its lifespan.
    from unified_api.main import app

    built = spec.body_builder(context)
    path = built.path_override or spec.path

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://internal",
        timeout=DEFAULT_DISPATCH_TIMEOUT_S,
    ) as client:
        try:
            if built.files:
                resp = await client.request(
                    spec.method,
                    path,
                    data=built.form or {},
                    files=built.files,
                )
            else:
                resp = await client.request(
                    spec.method,
                    path,
                    json=built.json or {},
                )
        except httpx.HTTPError as exc:
            logger.warning("Launch dispatch to %s failed: %s", path, exc)
            return DispatchResult(status=502, body={"error": str(exc)}, job_id=None)

    body = _safe_json(resp)
    job_id = body.get("job_id") if not spec.synchronous else None
    return DispatchResult(status=resp.status_code, body=body, job_id=job_id)


__all__ = ["DispatchResult", "dispatch"]
