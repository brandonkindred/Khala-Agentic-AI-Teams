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
    no timestamps) when ``previous_code`` is not ``None``, that diff is
    non-empty, and it is strictly shorter, in characters, than ``current_code``
    itself. Otherwise returns ``current_code`` unchanged — this covers the
    no-previous-round case, a near-total-rewrite whose diff would be as large as
    or larger than just resending the full text, and the no-op case below.
    Never mutates either input.

    An *empty* diff (``previous_code == current_code``, so ``unified_diff``
    yields nothing) is deliberately excluded from the "diff is shorter" branch
    even though ``len("") < len(current_code)`` holds for any non-empty code.
    Returning it would render the caller's diff section as an explanatory
    preamble wrapping an empty fence — carrying no code at all — and because
    that section is far shorter than the full text, the caller's own
    shorter-section-wins comparison (``RefinementAgent.run``) would then
    *select* it, sending the model "reconstruct the current file from context"
    with nothing to reconstruct from. Falling back to the full text keeps a
    no-op round byte-identical to a first round.
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

    if diff and len(diff) < len(current_code):
        return diff

    return current_code


__all__ = ["diff_or_full"]
