"""Shared helpers for coding_team API routes."""

from __future__ import annotations

import os
from typing import Optional, Protocol

from fastapi import HTTPException


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
