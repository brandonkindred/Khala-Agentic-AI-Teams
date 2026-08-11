"""Shared, model-shape-agnostic mechanics for projecting to/from ``AgentManifest``.

Both the Studio (`agent_team_studio.agent_studio`) and agentic
(`agent_team_studio.agentic_team_provisioning`) authoring surfaces build
:class:`~agent_registry.models.AgentManifest` instances using the same
handful of id-construction primitives (slugging, hashed-slug ids, round-trip
validation). This module single-sources those primitives — as a public,
stable home independent of either surface's internals — so each surface only
owns its own team-specific data (digest lengths, id formats), not the
mechanics themselves. Marker-tag filtering and the other generated-agent
runtime constants shared by both surfaces live in
:mod:`agent_team_studio.manifest_shared`.
"""

from __future__ import annotations

import hashlib
import re

from .models import AgentManifest


def slug(value: str | None, max_len: int = 40) -> str:
    """Lowercase, hyphenated slug of ``value``, bounded to ``max_len`` chars.

    Preconditions: ``max_len > 0`` (raises ``ValueError`` otherwise).
    Postconditions: returns ``value`` lowercased with runs of non-alphanumeric
        characters collapsed to a single ``-``, leading/trailing ``-`` stripped,
        and truncated to at most ``max_len`` characters (with any ``-`` left
        dangling by truncation also stripped). Returns ``"agent"`` when
        ``value`` is ``None``, empty, or slugs to nothing (e.g. all-symbol
        input).
    """
    if max_len <= 0:
        raise ValueError("slug: max_len must be positive")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return (cleaned[:max_len] if cleaned else "agent").rstrip("-")


def hash_suffix(value: str, length: int) -> str:
    """Stable hex digest prefix of ``value``.

    Preconditions: ``value`` is a string; ``0 < length <= 64`` (raises
        ``ValueError`` otherwise).
    Postconditions: returns ``hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]``
        — deterministic for a given ``value``, and callers choose their own
        ``length`` (and their own slug/id format around it).
    """
    if not 0 < length <= 64:
        raise ValueError("hash_suffix: length must satisfy 0 < length <= 64")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def revalidate(manifest: AgentManifest) -> AgentManifest:
    """Round-trip ``manifest`` through JSON to guarantee it is fully validated.

    Preconditions: ``manifest`` is an ``AgentManifest`` instance.
    Postconditions: returns an equal ``AgentManifest`` that is guaranteed
        JSON-safe and re-validated end to end.
    """
    return AgentManifest.model_validate(manifest.model_dump(mode="json"))
