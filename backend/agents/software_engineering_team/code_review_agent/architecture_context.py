"""Shared rendering of a ``SystemArchitecture`` into review-prompt text.

Extracted so both the per-chunk excerpt (``coordinator.py``) and the
once-per-submission architecture-consistency pass (``architecture_consistency_pass.py``)
render ``components``/``decisions`` identically instead of each carrying its
own copy of this logic.
"""

from __future__ import annotations

from typing import List

from software_engineering_team.shared.models import SystemArchitecture


def render_architecture_context(architecture: SystemArchitecture) -> str:
    """Render an architecture object's structured fields into prompt text.

    Folds in ``components`` (module/service responsibilities) and ``decisions``
    (ADRs) alongside ``overview`` -- the concrete signal an architecture-
    consistency check needs; ``overview`` prose alone rarely names a boundary
    or a taken decision precisely enough to judge a contradiction. The full
    ``architecture_document`` is deliberately NOT included here (it can be
    arbitrarily large); callers that can afford it (the once-per-submission
    pass) inline it separately alongside this rendering.

    Postconditions:
        - Returns the overview/components/decisions sections that have
          content, joined by blank lines, in that order. Returns "" when
          ``architecture`` carries none of the three. Never raises: a
          malformed ``decisions`` entry (not a dict, or missing keys) is
          rendered from whatever fields are present, or skipped if it is not
          a dict at all.
    """
    parts: List[str] = []
    if architecture.overview:
        parts.append(architecture.overview)
    if architecture.components:
        comp_lines = []
        for c in architecture.components:
            label = f"- {c.name} ({c.type})" if c.type else f"- {c.name}"
            if c.description:
                label += f": {c.description}"
            comp_lines.append(label)
        if comp_lines:
            parts.append("Components:\n" + "\n".join(comp_lines))
    if architecture.decisions:
        decision_lines = []
        for d in architecture.decisions:
            if not isinstance(d, dict):
                continue
            title = d.get("title") or d.get("id") or "Decision"
            detail = d.get("decision") or d.get("description") or ""
            decision_lines.append(f"- {title}: {detail}" if detail else f"- {title}")
        if decision_lines:
            parts.append("Architecture decisions:\n" + "\n".join(decision_lines))
    return "\n\n".join(p for p in parts if p.strip())
