"""Shared helpers for coding_team API routes."""

from __future__ import annotations

import logging
import os
from typing import Optional, Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

_JOB_SERVICE_UNAVAILABLE_DETAIL = (
    "Job service unavailable (transport error); retry shortly."
)


class _HasGitHubToken(Protocol):
    github_token: Optional[str]


def resolve_github_token(request: _HasGitHubToken) -> str:
    """Resolve the GitHub token for a route request, falling back to the environment.

    Preconditions:
        - ``request`` exposes an optional ``github_token`` field.
    Postconditions:
        - Returns ``request.github_token`` when set, else the ``GITHUB_TOKEN``
          environment variable. Raises ``HTTPException(400)`` when neither is set.
    """
    token = request.github_token or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=400, detail="GITHUB_TOKEN not configured")
    return token


def register_job_service_unavailable_handlers(app: FastAPI) -> None:
    """Map exhausted job-service transport errors to HTTP 503 on ``app``.

    Preconditions:
        - ``app`` is a FastAPI application that serves routes which call
          ``JobServiceClient`` (or wrappers) synchronously or asynchronously.
    Postconditions:
        - Unhandled ``httpx.TransportError`` from those routes become JSON 503
          responses with a retryable detail, instead of uvicorn ASGI 500s.
        - Idempotent: re-registering replaces the same handler slot.
    """

    @app.exception_handler(httpx.TransportError)
    async def _job_service_transport_unavailable(
        request: Request, exc: httpx.TransportError
    ) -> JSONResponse:
        logger.warning(
            "Job service transport error on %s %s: %s",
            request.method,
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"detail": _JOB_SERVICE_UNAVAILABLE_DETAIL},
        )
