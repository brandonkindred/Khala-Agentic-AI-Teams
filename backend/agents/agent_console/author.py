"""Derive a stable author handle from the shared AuthorProfile.

Phase 3 uses this as a placeholder for real auth: every saved input and
run is tagged with a best-effort handle so that when auth lands, a
migration can map these rows to user ids without data loss.

The profile already exists for the blogging pipeline
(:mod:`blogging.author_profile`). We reuse its loader so the contract
stays single-sourced.
"""

from __future__ import annotations

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

ANONYMOUS = "anonymous"


@lru_cache(maxsize=1)
def resolve_author() -> str:
    """Return a stable author handle, cached per process.

    Priority:
      1. ``identity.short_name`` from the loaded :class:`AuthorProfile`
      2. ``identity.full_name``
      3. the literal ``"anonymous"``

    Never raises — a missing profile, an import failure, or an unreadable
    YAML all fall back to ``"anonymous"`` so the invoke and save paths
    are never blocked by profile problems.
    """
    try:
        from blogging.author_profile import load_author_profile  # noqa: PLC0415
    except Exception:
        logger.debug("author_profile module unavailable; using %s", ANONYMOUS, exc_info=True)
        return ANONYMOUS

    try:
        profile = load_author_profile()
    except Exception:
        logger.debug("author_profile load failed; using %s", ANONYMOUS, exc_info=True)
        return ANONYMOUS

    identity = getattr(profile, "identity", None)
    if identity is None:
        return ANONYMOUS

    short = (getattr(identity, "short_name", "") or "").strip()
    if short:
        return short
    full = (getattr(identity, "full_name", "") or "").strip()
    if full:
        return full
    return ANONYMOUS
