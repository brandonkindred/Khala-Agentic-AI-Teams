"""Branding API — health check endpoint."""

from __future__ import annotations

from typing import Dict

from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> Dict[str, str]:
    """Report liveness of the branding API.

    Preconditions:
        None — the endpoint takes no input and touches no external state.
    Postconditions:
        Always returns ``{"status": "ok"}`` with HTTP 200.
    """
    return {"status": "ok"}
