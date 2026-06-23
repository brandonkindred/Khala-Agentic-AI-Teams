"""Typed, defensive readers for environment-variable tuning knobs.

Every reader returns a value of the documented type, applies optional
floor/ceiling clamping, and falls back to ``default`` for a missing or
unparseable value — it never raises for a bad environment *value*. This centralizes the
``os.environ.get`` + ``try/except`` + ``max/min`` idiom that teams would
otherwise each re-implement (it grew up inside the SE team and is now shared).

Invariants:
    - A reader never raises for any environment *value* (garbage → ``default``).
    - The returned value lies within ``[floor, ceiling]`` whenever those bounds
      are supplied (the ``default`` itself is clamped, so an out-of-range default
      can never leak through).
"""

from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _clamp(value: float, floor: float | None, ceiling: float | None) -> float:
    """Clamp ``value`` to ``[floor, ceiling]`` (each bound optional).

    Preconditions:
        - When both bounds are given, ``floor <= ceiling`` (else the clamp order
          is ill-defined and the ceiling would always win).
    """
    if floor is not None and ceiling is not None and floor > ceiling:
        raise ValueError(f"floor ({floor}) must not exceed ceiling ({ceiling})")
    if floor is not None:
        value = max(floor, value)
    if ceiling is not None:
        value = min(ceiling, value)
    return value


def _require_default_in_bounds(default: float, floor: float | None, ceiling: float | None) -> None:
    """Enforce the precondition that ``default`` lies within ``[floor, ceiling]``.

    Raises (not ``assert``) so the contract holds under ``python -O``, where a
    caller bug would otherwise let an out-of-range default be silently clamped.
    """
    if floor is not None and default < floor:
        raise ValueError(f"default ({default}) must be >= floor ({floor})")
    if ceiling is not None and default > ceiling:
        raise ValueError(f"default ({default}) must be <= ceiling ({ceiling})")


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean env var.

    Preconditions:
        - ``name`` is a non-empty environment variable name.
    Postconditions:
        - Returns ``True`` for ``true/1/yes/on`` and ``False`` for
          ``false/0/no/off`` (case-insensitive, whitespace-tolerant); ``default``
          for an unset or unrecognized value. Never raises.
    """
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


def env_int(name: str, default: int, floor: int | None = None, ceiling: int | None = None) -> int:
    """Read an int tuning knob, clamped to ``[floor, ceiling]`` when supplied.

    Preconditions:
        - ``default`` respects ``floor``/``ceiling`` when those are supplied.
    Postconditions:
        - Returns ``default`` (clamped) when the var is unset or unparseable;
          otherwise the parsed value clamped to the supplied bounds.
        - Never raises for an environment *value* (garbage → ``default``); raises
          ``ValueError`` only on a caller contract violation — a ``default``
          outside ``[floor, ceiling]``, or ``floor > ceiling``.
    """
    _require_default_in_bounds(default, floor, ceiling)
    raw = os.environ.get(name)
    if raw is None:
        return int(_clamp(default, floor, ceiling))
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        # Set-but-unparseable is a misconfiguration worth surfacing (unset is not).
        logger.warning("Invalid int for %s=%r; using default %d", name, raw, default)
        return int(_clamp(default, floor, ceiling))
    return int(_clamp(value, floor, ceiling))


def env_float(
    name: str, default: float, floor: float | None = None, ceiling: float | None = None
) -> float:
    """Read a float tuning knob, clamped to ``[floor, ceiling]`` when supplied.

    Preconditions:
        - ``default`` respects ``floor``/``ceiling`` when those are supplied.
    Postconditions:
        - Returns a finite float: ``default`` (clamped) when the var is unset,
          unparseable, or non-finite (``inf``/``nan``); otherwise the parsed value
          clamped to the supplied bounds.
        - Never raises for an environment *value*; raises ``ValueError`` only on a
          caller contract violation — a ``default`` outside ``[floor, ceiling]``,
          or ``floor > ceiling``.
    """
    _require_default_in_bounds(default, floor, ceiling)
    raw = os.environ.get(name)
    if raw is None:
        return float(_clamp(default, floor, ceiling))
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        logger.warning("Invalid float for %s=%r; using default %s", name, raw, default)
        return float(_clamp(default, floor, ceiling))
    if not math.isfinite(value):
        logger.warning("Non-finite float for %s=%r; using default %s", name, raw, default)
        value = float(default)
    return float(_clamp(value, floor, ceiling))
