"""Shared, dependency-free statistics helpers for the ``metrics`` package.

Both :mod:`dora` and :mod:`agent_rollup` need "median/percentile of a sample, ``None``
for an empty sample" — pure Python, no numpy. Centralized here so a future fix (e.g.
NaN/inf handling) reaches every consumer instead of risking silent divergence between
copies.
"""

from __future__ import annotations

import math
from typing import Optional


def median(values: list[float]) -> Optional[float]:
    """Median of ``values``; ``None`` for an empty list.

    Preconditions:
        - Every element of ``values`` is finite (no NaN/inf).
    Postconditions:
        - Returns ``None`` iff ``values`` is empty.
        - Otherwise returns the sorted-list midpoint, averaging the two middle
          elements when ``len(values)`` is even.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def p95(values: list[float]) -> Optional[float]:
    """95th percentile of ``values`` by nearest-rank; ``None`` for an empty list.

    No interpolation between neighboring ranks.

    Preconditions:
        - Every element of ``values`` is finite (no NaN/inf).
    Postconditions:
        - Returns ``None`` iff ``values`` is empty.
        - Otherwise returns ``sorted(values)[rank - 1]`` where
          ``rank = max(1, min(n, ceil(0.95 * n)))`` and ``n = len(values)`` — the single
          sample at ``n == 1``, the larger of two at ``n == 2``.
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    rank = max(1, min(n, math.ceil(0.95 * n)))
    return ordered[rank - 1]


__all__ = ["median", "p95"]
