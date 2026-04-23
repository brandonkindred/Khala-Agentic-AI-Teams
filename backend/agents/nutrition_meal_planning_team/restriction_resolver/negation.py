"""Negation-pattern detection for restriction inputs.

Users write ``"no cashews"`` / ``"avoid peanuts"`` / ``"gluten-free"``
/ ``"without dairy"`` when they mean the same thing as the bare
ingredient. The resolver runs negation detection first, strips the
marker, and then routes the residue through the normal cascade. The
original raw string is preserved on the resolved record.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Leading markers: ``no X``, ``avoid X``, ``without X``, ``free of X``.
_LEADING = re.compile(
    r"^\s*(no|avoid|without|free\s+of)\s+(?P<rest>.+?)\s*$",
    re.IGNORECASE,
)

# Trailing markers: ``X-free``, ``X free``. Match only when ``free`` is
# the last token so we don't strip from things like ``free-range eggs``.
_TRAILING = re.compile(
    r"^\s*(?P<rest>.+?)[\s-]+free\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Negation:
    """Result of negation detection."""

    is_negation: bool
    stripped: str  # the payload with the marker removed (empty if none)
    pattern: str = ""  # "leading" | "trailing" | ""


def detect(raw: str) -> Negation:
    """Return a :class:`Negation` describing whether ``raw`` is a
    negated restriction and what the bare payload is.

    ``stripped`` is untouched by normalization — the caller runs the
    existing normalizer on it as part of the cascade. This keeps the
    module dependency-free and easy to test.
    """
    if not raw or not raw.strip():
        return Negation(is_negation=False, stripped="")
    m = _LEADING.match(raw)
    if m:
        return Negation(is_negation=True, stripped=m.group("rest"), pattern="leading")
    m = _TRAILING.match(raw)
    if m:
        return Negation(is_negation=True, stripped=m.group("rest"), pattern="trailing")
    return Negation(is_negation=False, stripped=raw)
