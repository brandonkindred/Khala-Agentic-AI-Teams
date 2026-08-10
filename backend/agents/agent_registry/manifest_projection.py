"""Shared, model-shape-agnostic mechanics for projecting to/from ``AgentManifest``.

Both the Studio (`agent_team_studio.agent_studio`) and agentic
(`agent_team_studio.agentic_team_provisioning`) authoring surfaces build and
tear down :class:`~agent_registry.models.AgentManifest` instances using the
same handful of primitives (slugging, hashed-slug ids, marker-tag filtering,
round-trip validation). This module single-sources those primitives — as a
public, stable home independent of either surface's internals — so each
surface only owns its own team-specific data (marker sets, digest lengths, id
formats), not the mechanics themselves, and neither has to reach into the
other's private helpers to get them.
"""

from __future__ import annotations

import hashlib
import re

from .models import AgentManifest


def slug(value: str, max_len: int = 40) -> str:
    """Lowercase, hyphenated slug of ``value``, bounded to ``max_len`` chars.

    Preconditions: ``max_len > 0``.
    Postconditions: returns ``value`` lowercased with runs of non-alphanumeric
        characters collapsed to a single ``-``, leading/trailing ``-`` stripped,
        and truncated to at most ``max_len`` characters (with any ``-`` left
        dangling by truncation also stripped). Returns ``"agent"`` when
        ``value`` is ``None``, empty, or slugs to nothing (e.g. all-symbol
        input).
    """
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return (cleaned[:max_len] if cleaned else "agent").rstrip("-")


def hash_suffix(value: str, length: int) -> str:
    """Stable hex digest prefix of ``value``.

    Preconditions: ``value`` is a string; ``0 < length <= 64``.
    Postconditions: returns ``hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]``
        — deterministic for a given ``value``, and callers choose their own
        ``length`` (and their own slug/id format around it).
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def filter_marker_tags(tags: list[str] | None, markers: frozenset[str]) -> list[str]:
    """Strip internal bookkeeping tags out of a manifest's ``tags``.

    Preconditions: ``markers`` is the set of marker/plumbing tag values to drop.
    Postconditions: returns ``tags`` (or ``[]`` when ``tags`` is ``None``) with
        any value in ``markers`` removed, order preserved.
    """
    return [t for t in (tags or []) if t not in markers]


def revalidate(manifest: AgentManifest) -> AgentManifest:
    """Round-trip ``manifest`` through JSON to guarantee it is fully validated.

    Preconditions: ``manifest`` is an ``AgentManifest`` instance.
    Postconditions: returns an equal ``AgentManifest`` that is guaranteed
        JSON-safe and re-validated end to end.
    """
    return AgentManifest.model_validate(manifest.model_dump(mode="json"))
