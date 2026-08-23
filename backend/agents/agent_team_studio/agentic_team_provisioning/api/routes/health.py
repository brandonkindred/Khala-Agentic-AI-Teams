"""Liveness probe route for the agentic team provisioning API."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health():
    """Liveness probe.

    Preconditions: none.
    Postconditions: ``200`` with a static ``{"status": "ok", ...}`` body — never
        touches the store or infra, so it can't fail on their behalf.
    """
    return {"status": "ok", "service": "agentic-team-provisioning"}
