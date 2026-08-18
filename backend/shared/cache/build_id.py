"""Deploy/build identifier for shared cache namespaces.

When Redis-backed caches survive worker restarts, a deploy that changes
prompt text or review logic would otherwise keep serving pre-deploy outcomes
until TTL expiry. Consumers append the resolved build id to their namespace
so a new deploy is inherently a cold cache.

Env vars (first non-blank wins):

    KHALA_CACHE_BUILD_ID   Preferred explicit override for cache invalidation.
    KHALA_BUILD_ID         Generic build/deploy id (compose / CI can set this).
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

_ENV_KEYS = ("KHALA_CACHE_BUILD_ID", "KHALA_BUILD_ID")
# Namespace segments must not contain ``:`` (``shared.cache`` uses ``:`` as a
# delimiter in fully-qualified Redis keys). Keep the suffix URL/path-safe.
_SAFE_BUILD_ID = re.compile(r"^[A-Za-z0-9._@+/-]+$")


def cache_build_id() -> str:
    """Return the configured cache build id, or ``\"\"`` when unset.

    Preconditions:
        - None (reads only from the process environment).
    Postconditions:
        - Returns a non-empty string with no ``:`` when a recognized env var is
          set to a non-blank, safe value.
        - Returns ``\"\"`` when unset/blank, or when the value contains ``:`` /
          other unsafe characters (fail closed so a hostile env cannot break
          key layout). Unsafe non-blank values log a warning so operators are
          not left thinking deploy cold-cache is active when it is not.
    """
    for key in _ENV_KEYS:
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        if ":" in raw or not _SAFE_BUILD_ID.fullmatch(raw):
            logger.warning(
                "shared.cache: ignoring unsafe %s=%r "
                "(must match [A-Za-z0-9._@+/-]+ and must not contain ':'); "
                "cache namespaces stay at their static stems until a safe "
                "build id is set",
                key,
                raw,
            )
            return ""
        return raw
    return ""


def with_cache_build_id(base_namespace: str) -> str:
    """Append the build id to ``base_namespace`` when one is configured.

    Preconditions:
        - ``base_namespace`` is a non-empty namespace stem (e.g. ``cr:chunk:v2``).
    Postconditions:
        - Returns ``base_namespace`` unchanged when no build id is configured.
        - Returns ``\"{base_namespace}:{build_id}\"`` when a build id is set, so
          a deploy that changes ``KHALA_BUILD_ID`` / ``KHALA_CACHE_BUILD_ID``
          addresses a disjoint keyspace (prior entries TTL out).
    """
    if not base_namespace:
        raise ValueError("base_namespace must be non-empty")
    build_id = cache_build_id()
    if not build_id:
        return base_namespace
    return f"{base_namespace}:{build_id}"
