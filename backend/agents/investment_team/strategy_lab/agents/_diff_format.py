"""Compact code-diff formatting for round-over-round strategy refinement.

First implementation step toward letting ``refinement.py`` resend only the
delta between refinement rounds instead of the full strategy code every
round. Standalone utility, not yet wired into any prompt path.
"""

from __future__ import annotations

import difflib


def diff_or_full(previous_code: str | None, current_code: str) -> str:
    """Render a compact unified diff between two code strings, or the full text.

    Preconditions: ``current_code`` is a string (the current round's
    strategy code); ``previous_code`` is either ``None`` (no prior round
    exists) or a string (the previous round's strategy code).

    Postconditions: returns a unified-diff-style string (``difflib.unified_diff``,
    no timestamps) when ``previous_code`` is not ``None`` and that diff is
    strictly shorter, in characters, than ``current_code`` itself. Otherwise
    returns ``current_code`` unchanged — this covers both the no-previous-round
    case and a near-total-rewrite whose diff would be as large as or larger
    than just resending the full text. Never mutates either input.
    """
    if previous_code is None:
        return current_code

    diff = "".join(
        difflib.unified_diff(
            previous_code.splitlines(keepends=True),
            current_code.splitlines(keepends=True),
            fromfile="previous_round",
            tofile="current_round",
        )
    )

    if len(diff) < len(current_code):
        return diff

    return current_code


__all__ = ["diff_or_full"]
