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

    Preconditions:
        ``default`` is an int; ``minimum`` is None or an int.
    Postconditions:
        Returns an int; equals *default* when the var is unset or unparseable;
        never returns below *minimum* when *minimum* is given.
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

    Preconditions:
        ``default`` is a float.
    Postconditions:
        Returns a float; equals *default* when the var is unset/unparseable,
        and also when ``positive`` is True and the parsed value is <= 0.
    """
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    if positive and value <= 0:
        return default
    return value


def env_bool(name: str, default: bool = False) -> bool:
    """Parse a boolean env var.

    Preconditions:
        None.
    Postconditions:
        Returns True when the var is one of ``1/true/yes/on`` (case-insensitive);
        returns *default* when the var is unset; False otherwise.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
