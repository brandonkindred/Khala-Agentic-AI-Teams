"""Plain-text unified JSON diff helper.

Used by :mod:`unified_api.routes.agent_console_diff`. Two JSON payloads
are pretty-printed with sorted keys, then fed to :func:`difflib.unified_diff`.
This keeps the backend O(n), the transport text, and the frontend dumb.
"""

from __future__ import annotations

import difflib
import json
from typing import Any


def _pretty(value: Any) -> list[str]:
    """Deterministic pretty-print for diff input."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False).splitlines()


def unified_json_diff(
    left: Any,
    right: Any,
    *,
    left_label: str = "left",
    right_label: str = "right",
    context: int = 3,
) -> tuple[str, bool]:
    """Return ``(unified_diff_text, is_identical)``.

    ``unified_diff_text`` is a single string with ``\\n`` separators.
    ``is_identical`` is True iff the pretty-printed sides match exactly.
    """
    left_lines = _pretty(left)
    right_lines = _pretty(right)
    if left_lines == right_lines:
        return "", True
    diff = difflib.unified_diff(
        left_lines,
        right_lines,
        fromfile=left_label,
        tofile=right_label,
        n=context,
        lineterm="",
    )
    return "\n".join(diff), False
