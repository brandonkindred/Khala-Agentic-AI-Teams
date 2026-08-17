"""Deterministic phase-input hashing for branding-team pipeline memoization.

A pure, side-effect-free primitive used to detect when a pipeline phase's
inputs are unchanged from a prior run, so a future cache layer (Story 2a,
Step 2) can skip re-running that phase on interactive re-runs. No cache
container, orchestrator wiring, or conversation-layer wiring lives here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Mapping

from pydantic import BaseModel

from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import BrandingMission, BrandPhase

__all__ = ["phase_input_hash"]


def phase_input_hash(
    phase: BrandPhase,
    mission: BrandingMission,
    upstream_outputs: Mapping[BrandPhase, BaseModel],
) -> str:
    """Deterministic SHA-256 hash of one pipeline phase's inputs.

    Today every phase's task is seeded with the *entire* serialized mission
    (``orchestrator._phase_task`` calls ``serialize_mission(mission)``
    unconditionally — there is no per-phase mission-field subsetting
    anywhere in this codebase), so this primitive conservatively hashes the
    full mission for every phase rather than guessing at a narrower
    per-phase field set. Over-hashing only lowers a future cache's hit
    rate; under-hashing could make it return a stale hit. If a
    phase -> mission-fields mapping is introduced later, this function's
    body can be narrowed without changing its signature or callers.

    Preconditions:
        - ``phase`` is one of the five runnable pipeline phases in
          ``PHASE_ORDER``; ``BrandPhase.COMPLETE`` is not accepted.
        - ``mission`` is a constructed ``BrandingMission``.
        - ``upstream_outputs`` maps zero or more upstream ``BrandPhase``
          members to their completed output models.
    Postconditions:
        - Returns a 64-character lowercase hex digest.
        - Equal ``(phase, mission, upstream_outputs)`` inputs — including
          equal-but-distinct object instances, and ``upstream_outputs``
          mappings built with the same entries in different insertion
          order — produce an identical digest across separate calls and
          separate process runs: the digest is computed from a canonical
          JSON serialisation with sorted keys, never from ``hash()``/``id()``
          or raw dict iteration order.
        - Changing any field of ``mission``, changing any field of any
          value in ``upstream_outputs``, or adding/removing/replacing an
          ``upstream_outputs`` entry changes the digest.
    Invariants:
        - Function is pure: no side effects, no I/O, no dependence on
          mutable module state.
    """
    if phase not in PHASE_ORDER:
        raise ValueError(f"{phase!r} is not a runnable branding phase")

    payload = {
        "phase": phase.value,
        "mission": mission.model_dump(mode="json"),
        "upstream_outputs": {
            upstream_phase.value: output.model_dump(mode="json")
            for upstream_phase, output in upstream_outputs.items()
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
