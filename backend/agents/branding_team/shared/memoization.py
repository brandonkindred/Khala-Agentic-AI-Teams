"""Deterministic phase-input hashing for branding-team pipeline memoization.

A pure, side-effect-free primitive used to detect when a pipeline phase's
inputs are unchanged from a prior run, so a future cache layer (Story 2a,
Step 2) can skip re-running that phase on interactive re-runs. No cache
container, orchestrator wiring, or conversation-layer wiring lives here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping, Optional

from pydantic import BaseModel

from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import BrandingMission, BrandPhase

__all__ = ["phase_input_hash"]


def phase_input_hash(
    phase: BrandPhase,
    mission: BrandingMission,
    upstream_outputs: Mapping[BrandPhase, BaseModel],
    context_phases: Optional[tuple[BrandPhase, ...]] = None,
    mission_fields: Optional[frozenset[str]] = None,
) -> str:
    """Deterministic SHA-256 hash of one pipeline phase's inputs.

    ``upstream_outputs``, like ``mission``, can be filtered before hashing —
    ``upstream_outputs`` via ``context_phases``, ``mission`` via
    ``mission_fields`` — mirroring ``orchestrator._phase_task``'s filtering
    of ``prior_outputs`` by ``_PhaseSpec.context_phases``: a phase whose
    agents never reference some upstream phase's output, or some mission
    field, shouldn't have its cache entry invalidated by a change to that
    irrelevant input.

    Today no ``_PhaseSpec`` sets ``mission_fields`` (``orchestrator._phase_task``
    still calls ``serialize_mission(mission)`` unconditionally — there is no
    per-phase mission-field subsetting of the *prompt* anywhere in this
    codebase yet), so every current caller hashes the full mission, exactly
    as before this parameter existed. Over-hashing only lowers a future
    cache's hit rate; under-hashing could make it return a stale hit. When a
    phase -> mission-fields mapping is wired into ``_PHASE_SPEC``, this
    parameter lets that phase's cache key narrow to just the fields its
    agents' prompts actually reference, without changing this function's
    other behavior.

    Preconditions:
        - ``phase`` is one of the five runnable pipeline phases in
          ``PHASE_ORDER``; ``BrandPhase.COMPLETE`` is not accepted.
        - ``mission`` is a constructed ``BrandingMission``.
        - ``upstream_outputs`` maps zero or more upstream ``BrandPhase``
          members to their completed output models.
        - ``context_phases``, if not ``None``, lists the upstream
          ``BrandPhase``s whose ``upstream_outputs`` entries are relevant to
          ``phase`` (typically a ``_PhaseSpec.context_phases`` value) — the
          empty tuple ``()`` is a valid, meaningful value here ("none are
          relevant"), distinct from ``None`` ("not configured"). It need not
          be a subset of ``upstream_outputs``'s keys — an entry named in
          ``context_phases`` but absent from ``upstream_outputs`` is simply
          not hashed.
        - ``mission_fields``, if not ``None``, is an allowlist of
          ``BrandingMission`` field names relevant to ``phase`` (typically a
          future ``_PhaseSpec.mission_fields`` value) — the empty frozenset
          ``frozenset()`` is a valid, meaningful value here ("no mission
          field is relevant"), distinct from ``None`` ("not configured"),
          mirroring the ``context_phases``/empty-tuple convention above.
          Every name in ``mission_fields`` should be an actual
          ``BrandingMission`` field name; an unknown name is a caller bug
          and is not specially handled here — it is silently ignored by
          ``model_dump(include=...)``, the same way pydantic treats it.
    Postconditions:
        - Returns a 64-character lowercase hex digest.
        - When ``context_phases`` is ``None`` (the default — "not
          configured"), every entry in ``upstream_outputs`` is hashed —
          unchanged, backward-compatible behavior.
        - When ``context_phases`` is not ``None`` (including the empty
          tuple), only ``upstream_outputs`` entries whose key is in
          ``context_phases`` are hashed; a change to an ``upstream_outputs``
          entry whose key is *not* in ``context_phases`` does not change the
          digest. Passing ``()`` therefore hashes as if ``upstream_outputs``
          were empty, regardless of what it actually contains.
        - When ``mission_fields`` is ``None`` (the default — "not
          configured"), every field of ``mission`` is hashed — unchanged,
          backward-compatible behavior; this reproduces, byte-for-byte, the
          digest this function produced before ``mission_fields`` existed.
        - When ``mission_fields`` is not ``None`` (including the empty
          frozenset), only ``mission`` fields named in ``mission_fields``
          are hashed; a change to a ``mission`` field *not* named in
          ``mission_fields`` does not change the digest. Passing
          ``frozenset()`` therefore hashes as if ``mission`` contributed no
          fields at all.
        - Equal ``(phase, mission, upstream_outputs, context_phases,
          mission_fields)`` inputs — including equal-but-distinct object
          instances, and ``upstream_outputs``/``context_phases``/
          ``mission_fields`` built with the same entries in different order
          — produce an identical digest across separate calls and separate
          process runs: the digest is computed from a canonical JSON
          serialisation with sorted keys, never from ``hash()``/``id()`` or
          raw dict/tuple/set iteration order.
        - Changing any hashed field of ``mission`` (per the ``mission_fields``
          filtering rule above), or changing/adding/removing/replacing an
          ``upstream_outputs`` entry whose key is hashed (per the
          ``context_phases`` filtering rule), changes the digest.
    Invariants:
        - Function is pure: no side effects, no I/O, no dependence on
          mutable module state.
    """
    if phase not in PHASE_ORDER:
        raise ValueError(f"{phase!r} is not a runnable branding phase")

    if context_phases is not None:
        allowed = set(context_phases)
        upstream_outputs = {
            upstream_phase: output
            for upstream_phase, output in upstream_outputs.items()
            if upstream_phase in allowed
        }

    payload = {
        "phase": phase.value,
        "mission": mission.model_dump(mode="json", include=mission_fields),
        "upstream_outputs": {
            upstream_phase.value: output.model_dump(mode="json")
            for upstream_phase, output in upstream_outputs.items()
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
