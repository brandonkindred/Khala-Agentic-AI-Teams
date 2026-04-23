"""SPEC-006 resolver hook, split out for testing without strands.

Pure logic only. ``agent.py`` imports and applies this after both the
LLM merge and the structural fallback, so tests can exercise it
directly without the strands stack.
"""

from __future__ import annotations

import logging
import os

from ...models import ClientProfile

logger = logging.getLogger(__name__)


def apply_resolver(profile: ClientProfile) -> ClientProfile:
    """Populate ``profile.restriction_resolution`` when
    ``NUTRITION_RESTRICTION_RESOLVER=1`` is set.

    Mutates the profile in place and returns it. Raw lists are
    untouched. On resolver failure, logs a warning and returns the
    profile with its existing (default) resolution so the user's
    write still persists.
    """
    if os.environ.get("NUTRITION_RESTRICTION_RESOLVER", "0") != "1":
        return profile
    try:
        from ...restriction_resolver import resolve_restrictions

        profile.restriction_resolution = resolve_restrictions(
            profile.allergies_and_intolerances or [],
            profile.dietary_needs or [],
        )
    except Exception:
        logger.warning(
            "restriction resolver failed; leaving resolution untouched",
            exc_info=True,
        )
    return profile
