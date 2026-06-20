"""Typed, defensive readers for environment-variable tuning knobs.

Every reader returns a value of the documented type, applies optional
floor/ceiling clamping, and falls back to ``default`` for a missing or
unparseable value — it never raises into the caller. This centralizes the
``os.environ.get`` + ``try/except`` + ``max/min`` idiom that the SE
observability stores (trace/learnings/cost) and context-sizing knobs would
otherwise each re-implement.

Invariants:
    - A reader never raises for any environment value (garbage → ``default``).
    - The returned value lies within ``[floor, ceiling]`` whenever those bounds
      are supplied (the ``default`` itself is clamped, so an out-of-range default
      can never leak through).
"""

from __future__ import annotations

import math
import os

_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


def _clamp(value: float, floor: float | None, ceiling: float | None) -> float:
    if floor is not None:
        value = max(floor, value)
    if ceiling is not None:
        value = min(ceiling, value)
    return value


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
          otherwise the parsed value clamped to the supplied bounds. Never raises.
    """
    if floor is not None:
        assert default >= floor, "default must respect the floor"
    if ceiling is not None:
        assert default <= ceiling, "default must respect the ceiling"
    raw = os.environ.get(name)
    if raw is None:
        return int(default)
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return int(default)
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
          clamped to the supplied bounds. Never raises.
    """
    if floor is not None:
        assert default >= floor, "default must respect the floor"
    if ceiling is not None:
        assert default <= ceiling, "default must respect the ceiling"
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else float(default)
        if not math.isfinite(value):
            value = float(default)
    except (TypeError, ValueError):
        value = float(default)
    return float(_clamp(value, floor, ceiling))
