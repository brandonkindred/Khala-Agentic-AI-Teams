"""Pydantic models for the user profile and its artifact associations.

Design by Contract notes are stated per model/field. The overarching
invariant for this module: ``user_id`` is never empty — callers default
to :data:`user_profile.DEFAULT_USER_ID` (``"default"``) until real
authentication exists.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    """A single user's profile.

    Invariants:
        - ``user_id`` is non-empty and stable for the life of the profile.
        - ``preferences`` is always a JSON object (never ``None``).
    """

    user_id: str
    display_name: str = ""
    email: str = ""
    bio: str = ""
    preferences: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


class UserProfileUpdate(BaseModel):
    """Partial update payload for a profile.

    Preconditions:
        - Every field is optional; ``None`` means "leave unchanged".
    Postconditions:
        - Only the provided (non-``None``) fields are written.
    """

    display_name: Optional[str] = None
    email: Optional[str] = None
    bio: Optional[str] = None
    preferences: Optional[Dict[str, Any]] = None


class Association(BaseModel):
    """A link between a profile and an artifact produced by some team.

    Invariants:
        - ``(user_id, artifact_type, artifact_id)`` uniquely identifies the link.
        - The link only *references* the artifact; it never copies its data.
    """

    id: str
    user_id: str
    artifact_type: str
    team: str
    artifact_id: str
    label: str = ""
    role: str = "owner"
    created_at: str = ""
