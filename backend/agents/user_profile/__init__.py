"""User profile module — single cross-team profile and artifact registry.

Single-tenant today (one ``"default"`` profile, no auth). Other teams link
the artifacts they create to the profile by calling
:func:`record_association_safe` with their team slug and the artifact id;
nothing is copied, only referenced. The unified_api mounts the HTTP routes
(``/api/user-profile``) and registers :data:`SCHEMA` in its lifespan.

``ArtifactType`` enumerates the artifact kinds the registry understands so
producers and the UI agree on the strings.
"""

from __future__ import annotations

from .models import Association, AssociationList, UserProfile, UserProfileUpdate
from .postgres import SCHEMA
from .store import (
    DEFAULT_USER_ID,
    get_profile,
    list_associations,
    record_association,
    record_association_async,
    record_association_safe,
    remove_association,
    upsert_profile,
)


class ArtifactType:
    """Canonical artifact-type strings shared by producers and the UI."""

    BRAND = "brand"
    BLOG_POST = "blog_post"
    PROJECT = "project"
    AGENTIC_TEAM = "agentic_team"
    INTEGRATION = "integration"


__all__ = [
    "SCHEMA",
    "DEFAULT_USER_ID",
    "ArtifactType",
    "Association",
    "AssociationList",
    "UserProfile",
    "UserProfileUpdate",
    "get_profile",
    "upsert_profile",
    "record_association",
    "record_association_safe",
    "record_association_async",
    "list_associations",
    "remove_association",
]
