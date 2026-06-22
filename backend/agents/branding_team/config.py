"""Small, dependency-free env-parsing helpers shared across the branding team.

Centralizes the ``os.environ.get → parse → clamp/default`` shape that several
modules (the run executor, the market-research adapter, the assistant) would
otherwise each re-implement.
"""

from __future__ import annotations

import os
from typing import Optional


def env_int(name: str, default: int, *, minimum: Optional[int] = None) -> int:
    """Parse an int env var, falling back to *default* on garbage.

    Postconditions:
        Returns at least *minimum* when it is given.
    """
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    if minimum is not None and value < minimum:
        return minimum
    return value


def env_float(name: str, default: float, *, positive: bool = False) -> float:
    """Parse a float env var, falling back to *default* on garbage.

    When ``positive`` is True, non-positive values also fall back to *default*.
    """
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if positive and value <= 0:
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var (``1/true/yes/on`` → True), else *default*."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
