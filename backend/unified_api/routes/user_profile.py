"""User Profile API: review/update the single user profile and list the
artifacts (brands, blog posts, projects, agentic teams, integrations) that
teams have linked to it.

Single-tenant today: every endpoint operates on the ``"default"`` profile
(``user_profile.DEFAULT_USER_ID``). The ``user_id`` is centralized in the
store layer so real authentication can supply a real id later without
changing these routes.

Endpoints:
- GET    /api/user-profile                 -> current profile
- PUT    /api/user-profile                 -> update profile fields
- GET    /api/user-profile/associations    -> linked artifacts (optional ?artifact_type=)
- GET    /api/user-profile/integrations    -> integration status (pass-through)
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from user_profile import (
    Association,
    AssociationList,
    UserProfile,
    UserProfileUpdate,
    get_profile,
    list_associations,
    upsert_profile,
)
from user_profile.store import DEFAULT_USER_ID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user-profile", tags=["user-profile"])

#: Artifact-type filter values the registry understands. Restricting the query
#: param to this set makes FastAPI return 422 for anything else.
ArtifactTypeFilter = Literal["brand", "blog_post", "project", "agentic_team", "integration"]


class IntegrationStatus(BaseModel):
    """One row of GET /api/user-profile/integrations (mirrors the integrations list)."""

    id: str
    type: str
    enabled: bool
    channel: str | None = None


def _unavailable(exc: Exception) -> HTTPException:
    """Map a storage failure (e.g. Postgres disabled) to HTTP 503."""
    logger.warning("user_profile: storage unavailable: %s", exc, exc_info=True)
    return HTTPException(status_code=503, detail="User profile storage is unavailable.")


@router.get("", response_model=UserProfile)
def read_profile() -> UserProfile:
    """Return the current (default) profile, creating it on first access."""
    try:
        return get_profile(DEFAULT_USER_ID)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


@router.put("", response_model=UserProfile)
def update_profile(update: UserProfileUpdate) -> UserProfile:
    """Apply a partial update to the current profile and return it."""
    try:
        return upsert_profile(update, user_id=DEFAULT_USER_ID)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


@router.get("/associations", response_model=AssociationList)
def read_associations(
    artifact_type: Annotated[
        ArtifactTypeFilter | None,
        Query(description="Optional filter: brand, blog_post, project, agentic_team, integration."),
    ] = None,
) -> AssociationList:
    """List artifacts linked to the current profile, newest first."""
    try:
        items: list[Association] = list_associations(DEFAULT_USER_ID, artifact_type)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    return AssociationList(user_id=DEFAULT_USER_ID, associations=items)


@router.get("/integrations", response_model=list[IntegrationStatus])
def read_integrations() -> list[IntegrationStatus]:
    """Pass-through to the shared integrations list so the profile page can
    show integration status without duplicating that logic."""
    try:
        from unified_api.integrations_store import get_integrations_list

        return [IntegrationStatus(**item) for item in get_integrations_list()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("user_profile: integrations list unavailable: %s", exc, exc_info=True)
        return []
