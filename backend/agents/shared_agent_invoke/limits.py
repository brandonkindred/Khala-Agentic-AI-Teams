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
DEFAULT_MAX_WRITEBACK_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_EXEC_TIMEOUT_S = 60.0

# Headroom for the response envelope's framing + scalar metadata fields
# (``duration_ms``, ``trace_id``, ``error``, ``truncated``, ``timeout_hit``) and
# the line-bounded ``logs_tail`` (capped at 50 lines). The proxy's whole-response
# cap adds this to the per-field budgets (output + writeback) so a response whose
# ``output`` and ``tool_audit`` are each at their own cap is not falsely rejected
# by the framing overhead. 64 KiB comfortably covers 50 typical log lines + JSON.
RESPONSE_ENVELOPE_OVERHEAD_BYTES = 64 * 1024


def max_payload_bytes() -> int:
    return int(os.getenv("AGENT_INVOKE_MAX_PAYLOAD_BYTES", str(DEFAULT_MAX_PAYLOAD_BYTES)))


def max_output_bytes() -> int:
    return int(os.getenv("AGENT_INVOKE_MAX_OUTPUT_BYTES", str(DEFAULT_MAX_OUTPUT_BYTES)))


def max_writeback_bytes() -> int:
    """Independent cap for cognition control data (the tool audit) on the response.

    The per-field ``output`` cap (:func:`max_output_bytes`) must not be shared
    with the audit, or a near-cap ``output`` could starve the audit (or vice
    versa). Defaults to 1 MiB; override via ``AGENT_COGNITION_WRITEBACK_MAX_BYTES``.

    Parses defensively: an unset, unparseable, or non-positive override falls
    back to the default, so a malformed setting can never raise on the invoke
    response path or shrink the cap to a value that drops every audit entry.
    """
    raw = os.getenv("AGENT_COGNITION_WRITEBACK_MAX_BYTES")
    if raw is None:
        return DEFAULT_MAX_WRITEBACK_BYTES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_WRITEBACK_BYTES
    return value if value > 0 else DEFAULT_MAX_WRITEBACK_BYTES


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


def cap_tool_audit(entries: list, *, max_bytes: int) -> tuple[list, bool]:
    """Bound a list of audit entries to ``max_bytes`` of serialised JSON.

    Returns ``(entries, False)`` when the whole list fits. Otherwise keeps the
    longest leading prefix (oldest-first, so the start of the call sequence is
    preserved) whose serialisation *including* a trailing ``{"__truncated__":
    True, ...}`` marker still fits, returning ``(capped, True)``. Applied
    independently of the ``output`` cap so neither field can starve the other.

    The fit test serialises the actual candidate list each step (rather than
    hand-accounting separators), and uses ``json.dumps`` defaults — whose ``", "``
    item separators are *wider* than the compact separators Starlette emits — so
    the measured size is a conservative upper bound on the real response and the
    capped result is guaranteed to serialise within ``max_bytes`` (returning an
    empty list when even a lone truncation marker would not fit a tiny budget).
    """
    n = len(entries)
    if _safe_len(entries, max_bytes) <= max_bytes:
        return entries, False
    # Longest leading prefix that fits with a trailing truncation marker.
    kept: list = []
    for entry in entries:
        marker = {"__truncated__": True, "dropped": n - len(kept), "original_count": n}
        if _safe_len([*kept, entry, marker], max_bytes) > max_bytes:
            break
        kept.append(entry)
    # Shrink further until even the marker fits — a budget smaller than the marker
    # itself yields an empty list, never an over-cap result.
    while True:
        marker = {"__truncated__": True, "dropped": n - len(kept), "original_count": n}
        if _safe_len([*kept, marker], max_bytes) <= max_bytes:
            return [*kept, marker], True
        if not kept:
            return [], True
        kept.pop()


def _safe_len(obj: Any, over: int) -> int:
    """Serialised length of ``obj``; ``over + 1`` if it can't be serialised.

    The fallback forces an unserialisable candidate to be treated as over-budget
    (so it is dropped) rather than raising — audit entries are JSON-safe ToolCall
    dumps, so this is purely defensive.
    """
    try:
        return len(json.dumps(obj, default=str))
    except (TypeError, ValueError):  # pragma: no cover - defensive: entries are JSON-safe
        return over + 1
