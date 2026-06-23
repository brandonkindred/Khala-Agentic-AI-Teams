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
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from unified_api.integrations_store import get_integrations_list
from user_profile import (
    ArtifactType,
    Association,
    UserProfile,
    UserProfileUpdate,
    get_profile,
    list_associations,
    upsert_profile,
)
from user_profile.store import DEFAULT_USER_ID

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user-profile", tags=["user-profile"])


class IntegrationStatus(BaseModel):
    """One row of GET /api/user-profile/integrations (mirrors the integrations list)."""

    # Tolerate extra keys from get_integrations_list() so a future field added
    # there can't turn the whole list into a swallowed ValidationError → empty list.
    model_config = ConfigDict(extra="ignore")

    id: str
    type: str
    enabled: bool
    channel: str | None = None


class ProfileOverview(BaseModel):
    """Aggregated payload for the profile page — one round-trip instead of three."""

    profile: UserProfile
    associations: list[Association]
    integrations: list[IntegrationStatus]


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


@router.get("/associations", response_model=list[Association])
def read_associations(
    artifact_type: Annotated[
        ArtifactType | None,
        Query(description="Optional filter: brand, blog_post, project, agentic_team, integration."),
    ] = None,
) -> list[Association]:
    """List artifacts linked to the current profile, newest first."""
    try:
        return list_associations(DEFAULT_USER_ID, artifact_type)
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc


def _integrations_list() -> list[IntegrationStatus]:
    """Shared integration-status fetch (best-effort: empty list on failure)."""
    try:
        return [IntegrationStatus(**item) for item in get_integrations_list()]
    except Exception as exc:  # noqa: BLE001
        logger.warning("user_profile: integrations list unavailable: %s", exc, exc_info=True)
        return []


@router.get("/integrations", response_model=list[IntegrationStatus])
def read_integrations() -> list[IntegrationStatus]:
    """Pass-through to the shared integrations list so the profile page can
    show integration status without duplicating that logic."""
    return _integrations_list()


@router.get("/overview", response_model=ProfileOverview)
def read_overview() -> ProfileOverview:
    """Profile + linked artifacts + integration status in a single response, so
    the profile page loads in one round-trip instead of three."""
    try:
        profile = get_profile(DEFAULT_USER_ID)
        associations = list_associations(DEFAULT_USER_ID)
        # _integrations_list never raises today, but keeping it inside the try
        # means any future change can't bypass the 503 mapping with a raw 500.
        integrations = _integrations_list()
    except Exception as exc:  # noqa: BLE001
        raise _unavailable(exc) from exc
    return ProfileOverview(profile=profile, associations=associations, integrations=integrations)
