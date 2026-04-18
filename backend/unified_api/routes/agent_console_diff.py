"""
Agent Console Phase 3 — diff endpoint.

``POST /api/agents/diff`` compares two payloads (runs, saved inputs, or
inline JSON) and returns a unified-diff string plus a boolean flag. The
UI renders the diff inline with plain CSS; no client-side library.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from agent_console import (
    AgentConsoleStorageUnavailable,
    DiffRequest,
    DiffResult,
    get_store,
    unified_json_diff,
)
from agent_console.models import DiffSide

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agent-console"])


@router.post("/diff", response_model=DiffResult)
def diff(request: DiffRequest) -> DiffResult:
    try:
        left_data, left_label = _resolve_side(request.left, position="left")
        right_data, right_label = _resolve_side(request.right, position="right")
    except AgentConsoleStorageUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    diff_text, identical = unified_json_diff(
        left_data,
        right_data,
        left_label=left_label,
        right_label=right_label,
    )
    return DiffResult(
        unified_diff=diff_text,
        left_label=left_label,
        right_label=right_label,
        is_identical=identical,
    )


def _resolve_side(side: DiffSide, *, position: str) -> tuple[Any, str]:
    if side.kind == "inline":
        if side.data is None:
            raise HTTPException(
                status_code=422,
                detail=f"{position} is kind='inline' but has no 'data' field.",
            )
        return side.data, f"{position}:inline"

    if side.ref is None:
        raise HTTPException(
            status_code=422,
            detail=f"{position} is kind={side.kind!r} but has no 'ref' field.",
        )

    store = get_store()
    if side.kind == "run":
        run = store.get_run(side.ref)
        if run is None:
            raise HTTPException(status_code=404, detail=f"Unknown run: {side.ref}")
        payload = run.output_data if side.side == "output" else run.input_data
        label = f"run:{side.ref[:8]}:{side.side}"
        return payload, label

    if side.kind == "saved_input":
        saved = store.get_saved_input(side.ref)
        if saved is None:
            raise HTTPException(status_code=404, detail=f"Unknown saved input: {side.ref}")
        return saved.input_data, f"saved:{saved.name}"

    raise HTTPException(status_code=422, detail=f"Unsupported diff side kind: {side.kind}")
