"""Compatibility re-export of generated-agent constants plus marker-tag filtering.

Canonical entrypoint/schema/anatomy/cognition values live in
:mod:`shared.manifests`. This module re-exports them so existing Studio and
agentic call sites keep importing from here until those callers migrate.
``strip_marker_tags`` stays here: it is a local tag-filter helper, not a
construction constant.
"""

from __future__ import annotations

from shared.manifests import (
    AGENT_ANATOMY_REF,
    DEFAULT_RULE_PACKS,
    GENERATED_AGENT_ENTRYPOINT,
    GENERATED_AGENT_INPUT_REF,
    GENERATED_AGENT_OUTPUT_REF,
    default_cognition_block,
)

__all__ = [
    "AGENT_ANATOMY_REF",
    "DEFAULT_RULE_PACKS",
    "GENERATED_AGENT_ENTRYPOINT",
    "GENERATED_AGENT_INPUT_REF",
    "GENERATED_AGENT_OUTPUT_REF",
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
