"""Local tag-filter helper plus thin re-exports of shared construction constants.

Generated-agent entrypoint and invoke-schema refs are owned only by
:mod:`shared.manifests` — import ``GENERATED_AGENT_ENTRYPOINT``,
``GENERATED_AGENT_INPUT_REF``, and ``GENERATED_AGENT_OUTPUT_REF`` from there.

``strip_marker_tags`` stays here: it is a local tag-filter helper, not a
construction constant. Anatomy / rule-pack / cognition names are thin
single-sourced shims so existing ``manifest_shared`` imports keep working.
"""

from __future__ import annotations

from shared.manifests import (
    AGENT_ANATOMY_REF,
    DEFAULT_RULE_PACKS,
    default_cognition_block,
)

__all__ = [
    "AGENT_ANATOMY_REF",
    "DEFAULT_RULE_PACKS",
    "default_cognition_block",
    "strip_marker_tags",
]


def strip_marker_tags(tags: list[str], markers: frozenset[str]) -> list[str]:
    """Return ``tags`` excluding a caller-supplied marker set, order preserved.

    Preconditions:
        * ``tags`` and ``markers`` are non-``None`` (an empty collection is fine).
    Postconditions:
        * Returns a new list containing every entry of ``tags`` not present in
          ``markers``, in the same relative order; ``tags`` is not mutated.
    """
    return [t for t in tags if t not in markers]
