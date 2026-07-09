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

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_BRANDING", "branding-queue").strip() or "branding-queue"
WORKFLOW_ID_PREFIX = "branding-"

# Ordered phase value strings — must match ``BrandPhase`` values and the
# ``PHASE_ORDER`` in ``branding_team.graphs.shared``. The workflow indexes this to
# derive ``stop_idx`` from ``payload['target_phase']`` and to iterate the phases.
PHASE_SEQUENCE = [
    "strategic_core",
    "narrative_messaging",
    "visual_identity",
    "channel_activation",
    "governance",
]
