"""Temporal task queue and workflow IDs for the Branding team.

Invariants:
    - ``TASK_QUEUE`` is a non-empty string; the workflow dispatcher and the
      worker must agree on it, so both read it from here.
"""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_BRANDING", "branding-queue").strip() or "branding-queue"
WORKFLOW_ID_PREFIX = "branding-"
