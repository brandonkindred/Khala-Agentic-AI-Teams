"""Small shared runtime-config helpers for the Agent Cognition Core.

Centralizes two things that were being copy-pasted across the cognition modules:

* ``CHARS_PER_TOKEN`` — the repo's conservative token→char heuristic, used by every
  budget-capped text block (memory digest, knowledge-graph context).
* ``read_positive_int`` / ``read_int_with_floor`` — the two env-int parsing
  semantics the modules need (positive-or-default, and unset/garbage-or-floored).

Keeping these in one place means a change to the heuristic or the parsing policy
lands once instead of in eight modules. Pure stdlib — safe to import anywhere.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Conservative ~4-chars-per-token heuristic (matches the llm_service clients). A
# caller converts a token budget to a char budget by multiplying.
CHARS_PER_TOKEN = 4


def read_positive_int(name: str, default: int) -> int:
    """Parse a positive int env var, falling back to ``default``.

    Postconditions: returns the parsed value when ``>= 1``; an unset, non-integer,
    or non-positive value falls back to ``default``. A *set but unparseable* value
    is logged at WARNING so an operator can spot the misconfiguration (an unset
    value is normal and silent).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; falling back to %d", name, raw, default)
        return default
    return value if value >= 1 else default


def read_int_with_floor(name: str, default: int, floor: int) -> int:
    """Parse an int env var, flooring a too-small value rather than rejecting it.

    Postconditions: an unset value falls back to ``default`` silently; a *set but
    unparseable* value falls back to ``default`` and is logged at WARNING; a parsed
    value is returned as ``max(floor, value)`` (so an operator can lower a cadence
    but not below a safe minimum).
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; falling back to %d", name, raw, default)
        return default
    return max(floor, value)
