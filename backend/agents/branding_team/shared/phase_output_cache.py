"""In-memory phase-output cache for branding-team pipeline memoization.

A small, dict-backed cache container keyed by ``BrandPhase``, storing
``(input_hash, output)`` entries. Paired with ``phase_input_hash``
(``shared/memoization.py``), this lets a caller detect an unchanged phase
input and skip re-running that phase. No cache is consumed here — this
module only provides the container and its ``get``/``put`` helpers.
``orchestrator.run`` consumes it (Story 2b) on the thread path only via its
optional ``phase_cache`` parameter — the Temporal path calls
``orchestrator.run_single_phase`` directly and has no cache parameter to
receive one. The conversation/session layer (Story 2c Step 1) now carries a
cache across turns via a per-conversation registry in ``api/conversation.py``
(``_get_or_create_phase_cache``), but does not yet thread it into
``orchestrator.run`` (Story 2c Step 2).
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from pydantic import BaseModel

from branding_team.graphs.shared import PHASE_ORDER
from branding_team.models import BrandPhase

__all__ = ["PhaseOutputCache"]


class PhaseOutputCache:
    """A ``BrandPhase``-keyed cache of ``(input_hash, output)`` entries.

    Invariants:
        - At most one entry is stored per ``BrandPhase``; a ``put`` for a
          phase that already has an entry replaces it — there is no history.
        - No LLM or I/O side effects: state lives entirely in an in-memory
          dict for the lifetime of this instance.
    """

    def __init__(self) -> None:
        self._entries: Dict[BrandPhase, Tuple[str, BaseModel]] = {}

    @staticmethod
    def _validate_phase(phase: BrandPhase) -> None:
        if phase not in PHASE_ORDER:
            raise ValueError(f"{phase!r} is not a runnable branding phase")

    def get(self, phase: BrandPhase, input_hash: str) -> Optional[BaseModel]:
        """Return the cached output for ``phase``, or ``None`` on miss.

        Preconditions:
            - ``phase`` is one of the five runnable pipeline phases in
              ``PHASE_ORDER``; ``BrandPhase.COMPLETE`` is not accepted.
            - ``input_hash`` is a hash produced by ``phase_input_hash`` for
              the same ``phase``.
        Postconditions:
            - Returns the output previously stored via ``put(phase,
              input_hash, output)`` when a stored entry exists for ``phase``
              and its stored hash equals ``input_hash`` (a hit).
            - Returns ``None`` when no entry exists for ``phase``, or when
              an entry exists but its stored hash differs from
              ``input_hash`` (a miss either way — never raises for a
              mismatched hash).
        """
        self._validate_phase(phase)
        entry = self._entries.get(phase)
        if entry is None:
            return None
        stored_hash, output = entry
        if stored_hash != input_hash:
            return None
        return output

    def put(self, phase: BrandPhase, input_hash: str, output: BaseModel) -> None:
        """Store ``output`` for ``phase`` keyed by ``input_hash``.

        Preconditions:
            - ``phase`` is one of the five runnable pipeline phases in
              ``PHASE_ORDER``; ``BrandPhase.COMPLETE`` is not accepted.
            - ``input_hash`` is a hash produced by ``phase_input_hash`` for
              the same ``phase``.
            - ``output`` is the phase's constructed output model.
        Postconditions:
            - ``get(phase, input_hash)`` subsequently returns ``output``
              (a hit) until this method is called again for ``phase``.
            - Any prior entry for ``phase``, regardless of its stored hash,
              is replaced.
        """
        self._validate_phase(phase)
        self._entries[phase] = (input_hash, output)
