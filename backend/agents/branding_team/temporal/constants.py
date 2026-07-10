"""Temporal task queue and workflow IDs for the Branding team.

Invariants:
    - ``TASK_QUEUE`` is a non-empty string; the workflow dispatcher and the
      worker must agree on it, so both read it from here.
    - ``PHASE_SEQUENCE`` lists the pipeline phases in execution order using their
      ``BrandPhase`` value strings. It lives here (not in ``models``) so the
      workflow can compute how far to run without importing pydantic/``models``
      into the deterministic workflow sandbox.
"""

import os
from typing import Optional

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_BRANDING", "branding-queue").strip() or "branding-queue"
WORKFLOW_ID_PREFIX = "branding-"

# Ordered phase value strings — must match ``BrandPhase`` values and the
# ``PHASE_ORDER`` in ``branding_team.graphs.shared`` (guarded by
# ``test_phase_sequence_matches_brand_phase_values``). The workflow indexes this to
# derive ``stop_idx`` from ``payload['target_phase']`` and to iterate the phases.
PHASE_SEQUENCE = [
    "strategic_core",
    "narrative_messaging",
    "visual_identity",
    "channel_activation",
    "governance",
]


def stop_index(target_phase: Optional[str]) -> int:
    """Return the 0-based index of the last phase to run for ``target_phase``.

    Preconditions:
        - ``target_phase`` is a ``BrandPhase`` value string or ``None``.
    Postconditions:
        - Returns ``PHASE_SEQUENCE.index(target_phase)`` for a runnable phase.
        - ``None`` or a non-runnable value (notably ``BrandPhase.COMPLETE``, which
          is a terminal state, not a pipeline phase) maps to the last index so the
          run covers every phase — mirroring the thread path, where
          ``phase_index(COMPLETE)`` returns ``len(PHASE_ORDER)`` and the graph runs
          all phases. This avoids a ``ValueError`` on ``target_phase="complete"``.
    """
    if not target_phase or target_phase not in PHASE_SEQUENCE:
        return len(PHASE_SEQUENCE) - 1
    return PHASE_SEQUENCE.index(target_phase)
