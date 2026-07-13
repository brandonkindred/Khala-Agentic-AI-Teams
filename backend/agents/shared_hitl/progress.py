"""Progress-value coercion for job-status responses.

A single reconciled helper so every team clamps a stored progress value into the
renderable ``[0, 100]`` range instead of letting a corrupt record drive an
out-of-range progress bar.
"""

from __future__ import annotations

from typing import Any, Optional


def coerce_progress(value: Any) -> Optional[int]:
    """Coerce a stored progress value to an int in ``[0, 100]``, or ``None``.

    Preconditions:
        - ``value`` is arbitrary (a stored record field; JSON may give a float,
          a numeric string, ``None``, or garbage).
    Postconditions:
        - Returns ``None`` for non-numeric input (``TypeError``/``ValueError``
          from ``int(value)``).
        - Otherwise returns ``min(max(int(value), 0), 100)`` — numeric values are
          truncated toward zero and clamped, so a corrupt record can never render
          an out-of-range bar.
    """
    try:
        return min(max(int(value), 0), 100)
    except (TypeError, ValueError):
        return None
