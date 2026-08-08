"""Shared, dependency-free environment-variable parsing.

The platform reads many numeric / boolean env vars. Before this module each team
hand-rolled its own "parse int/float with a default, optionally clamp to a
floor/ceiling" helper (several mutually-incompatible variants) plus dozens of
ad-hoc ``int(os.getenv(...))`` sites that crash on a garbage value instead of
falling back. This module is the single canonical parser, matching the contract
documented in ``docs/ENV_VARS.md``:

    unset / blank / unparseable  -> the documented default
    out of range                 -> clamped to the floor / ceiling

Leaf module: standard library only, so any layer (``shared.postgres``,
``llm_service``, every team) can import it without creating an import cycle.

Invariants:
    - No function raises on a malformed env value; misconfiguration degrades to
      the documented default rather than crashing startup.
"""

from __future__ import annotations

import math
import os
from typing import Optional

__all__ = ["env_flag_enabled", "env_flag_opt_in", "parse_float", "parse_int"]

_FALSY = frozenset({"false", "0", "no"})
_TRUTHY = frozenset({"true", "1", "yes", "on"})


def env_flag_enabled(env_name: str) -> bool:
    """Return a default-on boolean env toggle.

    Preconditions: ``env_name`` is a non-empty environment-variable name
        (enforced with an explicit ``ValueError`` so the check survives ``-O``).
    Postconditions: returns ``False`` only for an explicit ``"false"`` / ``"0"`` /
        ``"no"`` (case-insensitive, whitespace-tolerant); an unset, blank, or any
        other value means enabled. Never raises on the env value.
    """
    if not env_name:
        raise ValueError("env_name must be non-empty")
    return (os.environ.get(env_name) or "").strip().lower() not in _FALSY


def env_flag_opt_in(env_name: str) -> bool:
    """Return a default-OFF boolean env toggle (the inverse of ``env_flag_enabled``).

    Use for new, unproven, or behaviorally significant code paths that must stay
    inert until explicitly enabled — unlike ``env_flag_enabled``, an unset or
    blank value here means disabled.

    Preconditions: ``env_name`` is a non-empty environment-variable name
        (enforced with an explicit ``ValueError`` so the check survives ``-O``).
    Postconditions: returns ``True`` only for an explicit ``"true"`` / ``"1"`` /
        ``"yes"`` / ``"on"`` (case-insensitive, whitespace-tolerant); an unset,
        blank, or any other value means disabled. Never raises on the env value.
    """
    if not env_name:
        raise ValueError("env_name must be non-empty")
    return (os.environ.get(env_name) or "").strip().lower() in _TRUTHY


def parse_int(
    env_name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Parse an integer env var, defaulting and clamping defensively.

    Preconditions: ``env_name`` is non-empty; ``default`` is an ``int``; when both
        ``minimum`` and ``maximum`` are given, ``minimum <= maximum``. Violations
        raise ``ValueError`` (explicit, so the check survives ``-O``).
    Postconditions: returns the parsed value clamped to ``[minimum, maximum]``
        (whichever bounds are provided); an unset, blank, or unparseable value
        yields ``default`` (also clamped). Never raises on a bad env value.
    """
    if not env_name:
        raise ValueError("env_name must be non-empty")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum must be <= maximum")
    raw = os.environ.get(env_name)
    stripped = raw.strip() if raw is not None else ""
    if not stripped:
        value = default
    else:
        try:
            # stripped is a non-blank str here, so int() can only raise ValueError.
            value = int(stripped)
        except ValueError:
            value = default
    if minimum is not None and value < minimum:
        value = minimum
    if maximum is not None and value > maximum:
        value = maximum
    return value


def parse_float(
    env_name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    """Parse a float env var, defaulting and clamping defensively.

    Preconditions: ``env_name`` is non-empty; ``default`` is a finite number; when
        both ``minimum`` and ``maximum`` are given, ``minimum <= maximum``.
        Violations raise ``ValueError`` (explicit, so the check survives ``-O``).
    Postconditions: returns the parsed value clamped to ``[minimum, maximum]``
        (whichever bounds are provided); an unset, blank, unparseable, or
        non-finite (``inf``/``nan``) value yields ``default`` (also clamped). Never
        raises on a bad env value.
    """
    if not env_name:
        raise ValueError("env_name must be non-empty")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum must be <= maximum")
    # Normalize the default to float once so every fallback branch can return it
    # directly (mirrors parse_int's ``value = default``) while still honouring the
    # ``-> float`` postcondition even when the caller passes an int default.
    default = float(default)
    raw = os.environ.get(env_name)
    stripped = raw.strip() if raw is not None else ""
    if not stripped:
        value = default
    else:
        try:
            value = float(stripped)
        except ValueError:
            value = default
    # Reject inf/nan from the env: a non-finite value defeats clamp comparisons
    # (always False for nan) and can busy-loop/crash a consumer using it as an
    # interval. Fall back to the (finite) default.
    if not math.isfinite(value):
        value = default
    if minimum is not None and value < minimum:
        value = float(minimum)
    if maximum is not None and value > maximum:
        value = float(maximum)
    return value
