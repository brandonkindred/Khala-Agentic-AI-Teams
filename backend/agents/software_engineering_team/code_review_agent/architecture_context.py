"""Shared rendering of a ``SystemArchitecture`` into review-prompt text.

Extracted so both the per-chunk excerpt (``coordinator.py``) and the
once-per-submission architecture-consistency pass (``architecture_consistency_pass.py``)
render ``components``/``decisions`` identically instead of each carrying its
own copy of this logic. Also hosts the shared "is there enough architecture
evidence to run Part 1?" gate used by the standalone pass and the in-process
merged architecture/side-effect pass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

from shared.dev_models.models import SystemArchitecture

if TYPE_CHECKING:
    from .false_positive_filter import CodebaseIndex
    from .models import CodeReviewInput
    from .repo_reader import RepoReader


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


def architecture_document_text(architecture: Optional[SystemArchitecture]) -> str:
    """Flatten the optional architecture payload into inlined body text.

    Postconditions:
        - Returns ``""`` when ``architecture`` is ``None`` or has no document /
          rendered context content.
        - Otherwise returns document + structured context joined by blank lines
          (without fences or section headers). Never raises.
    """
    if architecture is None:
        return ""
    return "\n\n".join(
        p
        for p in (
            (architecture.architecture_document or "").strip(),
            render_architecture_context(architecture),
        )
        if p
    )


def architecture_evidence_available(
    input_data: CodeReviewInput,
    repo_reader: Optional[RepoReader] = None,
    index: Optional[CodebaseIndex] = None,
) -> bool:
    """Whether architecture / redundancy review has a verifiable evidence source.

    Without a formal architecture payload, the pass is asked to derive
    expectations from established repository structure. That requires either an
    attached ``repo_reader`` (off-diff files) or a nonempty ``existing_codebase``
    excerpt; otherwise tools only see the changed submission files and any
    architecture finding would be speculation.

    Postconditions:
        - Returns ``True`` when a nonempty architecture document/context is on
          the input, a ``repo_reader`` is attached, or a nonempty
          ``existing_codebase`` excerpt is available (on the input or a shared
          ``index``). Otherwise ``False``. Never raises.
    """
    if architecture_document_text(input_data.architecture):
        return True
    if repo_reader is not None:
        return True
    if (input_data.existing_codebase or "").strip():
        return True
    if index is not None and (index.existing_codebase or "").strip():
        return True
    return False
