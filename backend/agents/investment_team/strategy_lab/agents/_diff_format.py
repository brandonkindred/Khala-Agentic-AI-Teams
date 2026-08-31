"""Compact code-diff formatting for round-over-round strategy refinement.

Lets ``refinement.py`` (see ``RefinementAgent.run``) resend only the delta
between refinement rounds instead of the full strategy code every round,
falling back to the full text on the first round or a near-total rewrite.

There is no dict/spec analogue here on purpose: ``DesignAgent.revise()``
always sends the full spec JSON rather than diffing it, since (unlike the
code diffed here) the spec is the authoritative object the model must fully
reconstruct with no downstream cross-check against the true prior spec. See
``design.py``'s ``_render_spec_section`` and
``SPEC_RECONSTRUCTION_FIDELITY.md`` for the full rationale.
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

    diff = "\n".join(
        difflib.unified_diff(
            previous_code.splitlines(),
            current_code.splitlines(),
            fromfile="previous_round",
            tofile="current_round",
            lineterm="",
        )
    )

    if len(diff) < len(current_code):
        return diff

    return current_code


__all__ = ["diff_or_full"]
