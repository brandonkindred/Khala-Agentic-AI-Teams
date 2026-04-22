"""Size and timeout limits for the agent invoke path.

Shared by the unified API proxy (``unified_api/routes/agents.py``) and the
sandbox shim (``shared_agent_invoke/shim.py``) so both enforcement points use
the same defaults and env-var overrides. See GitHub issue #256.

Three axes are bounded:

* Request body size — hard cap, returns HTTP 413 on overflow.
* Execution time — wrapped in ``asyncio.wait_for``, returns HTTP 504.
* Response body size — serialised output is truncated with a flag.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import HTTPException, Request

DEFAULT_MAX_PAYLOAD_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_MAX_OUTPUT_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_EXEC_TIMEOUT_S = 60.0


def max_payload_bytes() -> int:
    return int(os.getenv("AGENT_INVOKE_MAX_PAYLOAD_BYTES", str(DEFAULT_MAX_PAYLOAD_BYTES)))


def max_output_bytes() -> int:
    return int(os.getenv("AGENT_INVOKE_MAX_OUTPUT_BYTES", str(DEFAULT_MAX_OUTPUT_BYTES)))


def default_exec_timeout_s() -> float:
    return float(os.getenv("AGENT_EXEC_TIMEOUT_S", str(DEFAULT_EXEC_TIMEOUT_S)))


async def read_json_capped(request: Request, *, max_bytes: int) -> Any:
    """Read the request body with a hard size cap.

    Returns ``{}`` for empty or malformed JSON (preserving the existing
    silent-fallback contract of the invoke path). Raises ``HTTPException(413)``
    if the body exceeds ``max_bytes`` — the streaming loop short-circuits as
    soon as the cap is hit, so large payloads do not materialise in memory.
    """
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) > max_bytes:
        raise HTTPException(status_code=413, detail=f"Payload exceeds {max_bytes} bytes")
    buf = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise HTTPException(status_code=413, detail=f"Payload exceeds {max_bytes} bytes")
    if not buf:
        return {}
    try:
        return json.loads(bytes(buf))
    except (ValueError, json.JSONDecodeError):
        return {}


def cap_output(value: Any, *, max_bytes: int) -> tuple[Any, bool]:
    """Return ``(value, False)`` if the serialised size fits, else a truncation envelope."""
    try:
        serialized = json.dumps(value, default=str)
    except (TypeError, ValueError):
        serialized = repr(value)
    if len(serialized) <= max_bytes:
        return value, False
    return (
        {
            "__truncated__": True,
            "preview": serialized[:max_bytes],
            "original_size": len(serialized),
        },
        True,
    )
