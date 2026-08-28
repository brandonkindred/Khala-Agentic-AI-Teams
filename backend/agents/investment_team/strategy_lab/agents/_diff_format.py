"""Compact code/spec-diff formatting for round-over-round strategy refinement.

Lets ``refinement.py`` (see ``RefinementAgent.run``) resend only the delta
between refinement rounds instead of the full strategy code every round,
falling back to the full text on the first round or a near-total rewrite.
``diff_spec_or_full`` provides the analogous fallback-to-full-text behavior
for the design-review spec JSON (dict-shaped) rather than code (string-shaped).
"""

from __future__ import annotations

import difflib
import json
from typing import Any


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


def _walk_dict_diff(previous: dict[str, Any], current: dict[str, Any], path: str) -> list[str]:
    """Recursively collect ``added``/``removed``/``changed`` lines for two dicts.

    Preconditions: ``previous`` and ``current`` are dicts; ``path`` is the
    dotted key-path prefix accumulated from enclosing dict levels (``""`` at
    the top level).

    Postconditions: returns a list of human-readable lines, one per leaf-level
    difference, each prefixed with ``added:``, ``removed:``, or ``changed:``
    and the dotted path to the differing key. A nested dict present under the
    same key on both sides is recursed into rather than reported as a single
    ``changed`` line, so only genuine leaf differences are listed. Returns an
    empty list when ``previous == current``. Never mutates either input.
    """
    lines: list[str] = []
    all_keys = sorted(set(previous) | set(current))

    for key in all_keys:
        key_path = f"{path}.{key}" if path else key

        if key not in previous:
            lines.append(f"added: {key_path}")
            continue
        if key not in current:
            lines.append(f"removed: {key_path}")
            continue

        prev_value = previous[key]
        curr_value = current[key]
        if isinstance(prev_value, dict) and isinstance(curr_value, dict):
            lines.extend(_walk_dict_diff(prev_value, curr_value, key_path))
        elif prev_value != curr_value:
            lines.append(f"changed: {key_path}: {prev_value!r} -> {curr_value!r}")

    return lines


def diff_spec_or_full(previous_spec: dict[str, Any] | None, current_spec: dict[str, Any]) -> str:
    """Render a compact added/removed/changed-keys diff between two spec dicts, or the full JSON.

    Preconditions: ``current_spec`` is a JSON-serializable dict (the current
    round's strategy spec); ``previous_spec`` is either ``None`` (no prior
    round exists) or a JSON-serializable dict (the previous round's spec).

    Postconditions: returns a structural diff string (one ``added:``/
    ``removed:``/``changed:`` line per differing key, recursing into nested
    dicts, dotted-path keys for nesting) when ``previous_spec`` is not
    ``None`` and that diff is strictly shorter, in characters, than
    ``current_spec`` rendered as pretty-printed JSON. Otherwise returns
    ``current_spec`` rendered as pretty-printed JSON
    (``json.dumps(current_spec, indent=2, sort_keys=True)``) unchanged —
    this covers both the no-previous-round case and a near-total-rewrite
    whose diff would be as large as or larger than just resending the full
    JSON. Never mutates either input.
    """
    full_json = json.dumps(current_spec, indent=2, sort_keys=True)

    if previous_spec is None:
        return full_json

    diff = "\n".join(_walk_dict_diff(previous_spec, current_spec, ""))

    if len(diff) < len(full_json):
        return diff

    return full_json


__all__ = ["diff_or_full", "diff_spec_or_full"]
