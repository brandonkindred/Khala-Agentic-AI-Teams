"""Shared dual-env-var base-URL resolution for agent-team adapters.

Several adapters (branding_team's market_research, social_media_marketing_team's
branding) each independently reimplemented the same shape: resolve a
service's base URL by preferring a unified default env var, falling back to a
team-specific override. This module is the single home for that idiom.
"""

from __future__ import annotations

import os
from typing import Optional

__all__ = ["resolve_base_url"]


def resolve_base_url(default_env: str, override_env: str) -> Optional[str]:
    """Resolve a base URL, preferring ``default_env`` over ``override_env``.

    Preconditions:
        - ``default_env`` and ``override_env`` are non-empty environment
          variable names.
    Postconditions:
        - Returns ``os.environ[default_env]`` when set to a non-empty value.
        - Otherwise returns ``os.environ[override_env]`` when set to a
          non-empty value.
        - Otherwise returns None.
    """
    assert default_env, "default_env must be non-empty"
    assert override_env, "override_env must be non-empty"
    return os.environ.get(default_env) or os.environ.get(override_env)
