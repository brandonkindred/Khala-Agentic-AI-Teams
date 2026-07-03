"""Persist the job-seeker profile as the career section of the central user profile.

The standing search criteria are stored under the ``"career"`` key of
``user_profiles.profile_json`` (the ``user_profile`` module's Postgres-backed
record), so the same profile the Job Matching pipeline consumes is visible on
the User Profile page. Saving also records a ``career`` artifact association,
which renders as the "Career" card there.

Invariants:
    * This module never mutates other sections of ``profile_json``; a save
      merges the career section over the existing mapping.
    * Reads degrade gracefully (``None``) when Postgres is unconfigured or
      unavailable, so the YAML loader fallback keeps offline runs working.
      Writes never degrade silently: an unavailable store raises
      :class:`CareerProfileUnavailableError`.
"""

from __future__ import annotations

import logging
from typing import Optional

from user_profile import (
    DEFAULT_USER_ID,
    ArtifactType,
    UserProfileUpdate,
    get_profile,
    record_association_safe,
    upsert_profile,
)

from .model import JobSeekerProfile

logger = logging.getLogger(__name__)

#: Key inside ``user_profiles.profile_json`` holding the job-seeker profile dump.
CAREER_SECTION_KEY = "career"

#: Team slug recorded on the career association.
_TEAM_SLUG = "job_matching"


class CareerProfileUnavailableError(RuntimeError):
    """Raised when the career profile cannot be persisted (Postgres unavailable)."""


def load_career_profile(user_id: str = DEFAULT_USER_ID) -> Optional[JobSeekerProfile]:
    """Return the career section of ``user_id``'s profile, or ``None``.

    Preconditions:
        * ``user_id`` is a non-empty string.
    Postconditions:
        * Returns a validated :class:`JobSeekerProfile` when the section exists
          and validates.
        * Returns ``None`` when the section is absent, the store is
          operationally unavailable (unconfigured/unreachable Postgres), or the
          stored section is malformed — the caller falls back to the YAML
          resolution chain. A malformed section is a broken postcondition of a
          prior save: it is logged at ERROR (never silently), but must not
          hard-fail every scan and profile read; re-saving repairs it.
    """
    assert user_id, "user_id must be non-empty"
    from shared_postgres import is_postgres_enabled

    if not is_postgres_enabled():
        return None
    try:
        section = get_profile(user_id).preferences.get(CAREER_SECTION_KEY)
    except Exception:  # noqa: BLE001 - operational read failure degrades to YAML fallback
        logger.warning("career_store: could not read user profile; falling back", exc_info=True)
        return None
    if section is None:
        return None
    try:
        return JobSeekerProfile.model_validate(section)
    except Exception:  # noqa: BLE001 - a corrupt stored section must not hard-fail every scan
        logger.error(
            "career_store: stored career section is invalid; falling back to the YAML "
            "chain — re-save the profile via PUT /profile to repair it",
            exc_info=True,
        )
        return None


def save_career_profile(
    profile: JobSeekerProfile, user_id: str = DEFAULT_USER_ID
) -> JobSeekerProfile:
    """Persist ``profile`` as the career section of ``user_id``'s user profile.

    Preconditions:
        * ``user_id`` is a non-empty string; ``profile`` is a validated model.
    Postconditions:
        * ``user_profiles.profile_json[CAREER_SECTION_KEY]`` equals the profile
          dump; every other ``profile_json`` section is preserved even under
          concurrent section writers (the merge is a single atomic server-side
          ``profile_json || patch`` statement, not a read-modify-write).
        * A ``career`` artifact association exists for the profile (best-effort;
          an association failure is logged, never raised).
        * Returns the saved profile (round-tripped from the persisted mapping).

    Raises:
        CareerProfileUnavailableError: When Postgres is unconfigured or the
            write fails operationally — the API surfaces this as a 503.
    """
    assert user_id, "user_id must be non-empty"
    from shared_postgres import is_postgres_enabled

    if not is_postgres_enabled():
        raise CareerProfileUnavailableError(
            "Career profile storage requires Postgres (set POSTGRES_HOST)."
        )
    try:
        # upsert_profile applies `preferences` as a single atomic server-side
        # shallow merge (profile_json || EXCLUDED.profile_json), so the career
        # section lands (and updated_at advances) without a read-modify-write and
        # without clobbering other sections — no separate ensure-row step needed.
        saved = upsert_profile(
            UserProfileUpdate(preferences={CAREER_SECTION_KEY: profile.model_dump(mode="json")}),
            user_id=user_id,
        )
    except Exception as exc:  # noqa: BLE001 - operational write failure -> typed error for the API
        raise CareerProfileUnavailableError(f"Could not persist career profile: {exc}") from exc
    record_association_safe(
        ArtifactType.CAREER,
        _TEAM_SLUG,
        f"career:{user_id}",
        user_id=user_id,
        label="Career profile",
    )
    return JobSeekerProfile.model_validate(saved.preferences[CAREER_SECTION_KEY])
