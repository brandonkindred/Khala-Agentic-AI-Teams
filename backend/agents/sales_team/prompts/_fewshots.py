"""Helper that renders structured few-shot exemplars into a textual block."""

from __future__ import annotations

import json
from typing import Any

FewShotExamples = list[tuple[dict[str, Any], dict[str, Any]]]


def render_fewshots(examples: FewShotExamples) -> str:
    """Render input/output pairs as a Markdown block for system-prompt embedding.

    Returns an empty string when ``examples`` is empty so the rendered system
    prompt is byte-identical to the no-fewshot baseline.
    """
    if not examples:
        return ""
    lines = ["", "## Examples", ""]
    for i, (inp, out) in enumerate(examples, 1):
        lines.append(f"**Example {i} — input:**")
        lines.append("```json")
        lines.append(json.dumps(inp, indent=2, sort_keys=True))
        lines.append("```")
        lines.append(f"**Example {i} — expected output:**")
        lines.append("```json")
        lines.append(json.dumps(out, indent=2, sort_keys=True))
        lines.append("```")
        lines.append("")
    return "\n".join(lines)
