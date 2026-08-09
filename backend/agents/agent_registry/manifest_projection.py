"""Shared, model-shape-agnostic mechanics for projecting to/from ``AgentManifest``.

Both the Studio (`agent_team_studio.agent_studio`) and agentic
(`agent_team_studio.agentic_team_provisioning`) authoring surfaces build and
tear down :class:`~agent_registry.models.AgentManifest` instances using the
same handful of primitives (hashed-slug ids, marker-tag filtering, round-trip
validation). This module single-sources those primitives so each surface only
owns its own team-specific data (marker sets, digest lengths, id formats), not
the mechanics themselves.
"""

from __future__ import annotations

import hashlib

from .models import AgentManifest


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
