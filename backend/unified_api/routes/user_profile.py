"""User Profile API: review/update the single user profile and list the
artifacts (brands, blog posts, projects, agentic teams, integrations) that
teams have linked to it.

Single-tenant today: every endpoint operates on the ``"default"`` profile
(``user_profile.DEFAULT_USER_ID``). The ``user_id`` is centralized in the
store layer so real authentication can supply a real id later without
changing these routes.

Endpoints:
- GET    /api/user-profile                 -> current profile
- PUT    /api/user-profile                 -> update profile fields (preferences merge key-by-key)
- GET    /api/user-profile/associations    -> linked artifacts (optional ?artifact_type=)
- GET    /api/user-profile/integrations    -> integration status (pass-through)
- GET    /api/user-profile/overview        -> profile + associations + integrations (one round-trip)
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
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


@contextmanager
def _storage_guard() -> Iterator[None]:
    """Map any storage failure (e.g. Postgres disabled) raised in the block to HTTP 503.

    Centralizes the read/write error contract so each endpoint body stays a
    single store call rather than repeating the same try/except.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001
        logger.warning("user_profile: storage unavailable: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail="User profile storage is unavailable.") from exc


@router.get("", response_model=UserProfile)
def read_profile() -> UserProfile:
    """Return the current (default) profile, creating it on first access."""
    with _storage_guard():
        return get_profile(DEFAULT_USER_ID)


@router.put("", response_model=UserProfile)
def update_profile(update: UserProfileUpdate) -> UserProfile:
    """Apply a partial update to the current profile and return it.

    Omitted (``None``) fields are left unchanged. Present scalar fields
    (display_name/email/bio) are written verbatim; a present ``preferences``
    dict is merged key-by-key into the stored object (top-level keys
    overwrite, absent keys survive — no key-deletion path), so callers
    should send only the keys they own.
    """
    with _storage_guard():
        return upsert_profile(update, user_id=DEFAULT_USER_ID)


@router.get("/associations", response_model=list[Association])
def read_associations(
    artifact_type: Annotated[
        ArtifactType | None,
        Query(description="Optional filter: brand, blog_post, project, agentic_team."),
    ] = None,
) -> list[Association]:
    """List artifacts linked to the current profile, newest first."""
    with _storage_guard():
        return list_associations(DEFAULT_USER_ID, artifact_type)


def _integrations_list() -> list[IntegrationStatus]:
    """Shared integration-status fetch (best-effort).

    Resilient per item: one malformed integration entry (missing a required
    field) is skipped and logged rather than discarding every other integration;
    a failure to fetch the list at all yields an empty list.
    """
    try:
        raw = get_integrations_list()
    except Exception as exc:  # noqa: BLE001
        logger.warning("user_profile: integrations list unavailable: %s", exc, exc_info=True)
        return []
    items: list[IntegrationStatus] = []
    for item in raw:
        try:
            items.append(IntegrationStatus(**item))
        except Exception as exc:  # noqa: BLE001
            logger.warning("user_profile: skipping invalid integration item %r: %s", item, exc, exc_info=True)
    return items


@router.get("/integrations", response_model=list[IntegrationStatus])
def read_integrations() -> list[IntegrationStatus]:
    """Pass-through to the shared integrations list so the profile page can
    show integration status without duplicating that logic."""
    return _integrations_list()


@router.get("/overview", response_model=ProfileOverview)
def read_overview() -> ProfileOverview:
    """Profile + linked artifacts + integration status in a single response, so
    the profile page loads in one round-trip instead of three."""
    # _integrations_list never raises today, but keeping it inside the guard means
    # any future change can't bypass the 503 mapping with a raw 500.
    with _storage_guard():
        return ProfileOverview(
            profile=get_profile(DEFAULT_USER_ID),
            associations=list_associations(DEFAULT_USER_ID),
            integrations=_integrations_list(),
        )
