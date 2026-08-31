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


def raise_if_checkout_occupied(repo_path: str) -> None:
    """Raise 409 when another live job is already using ``repo_path``.

    Shared by ``/run-from-github`` and ``/pulls/{pr_number}/address-comments``:
    both admit onto an operator-pinned, unnamespaced checkout that is shared
    across every issue/PR of a repo, so each must reject admission when a
    sibling job (any issue or PR) is already running on the SAME checkout.
    Callers hold their own checkout-admission lock around this call.

    Preconditions:
        - ``repo_path`` is the (already-stripped) checkout path being admitted
          onto; non-empty, per :func:`coding_team_main.get_running_job_on_checkout`.
    Postconditions:
        - Returns ``None`` when no sibling job is running on ``repo_path``.
        - Raises ``HTTPException(409)`` when one is, with a detail naming the
          sibling job id and its PR/issue label. Never raises otherwise.
    """
    from software_engineering_team.api import coding_team_main as _main

    sibling = _main.get_running_job_on_checkout(repo_path)
    if sibling is None:
        return
    sib_ctx = sibling.get("github_context") or {}
    if "pr_number" in sib_ctx:
        sib_label = f"PR #{sib_ctx.get('pr_number')}"
    else:
        sib_label = f"issue #{sib_ctx.get('issue_number', '?')}"
    raise HTTPException(
        status_code=409,
        detail=(
            f"job {sibling.get('job_id')} ({sib_label}) is still "
            f"running on checkout {repo_path}; retry after it finishes"
        ),
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
