"""Temporal task queue and workflow IDs for the coding team.

Invariants:
    - ``TASK_QUEUE`` is a non-empty string; the workflow dispatcher
      (``start_workflow``) and the worker (``worker``) must agree on it, so both
      read it from here.
    - Imported only by ``worker``/``start_workflow`` — never by the workflow-
      defining ``__init__``, so the ``os.getenv`` below never runs inside the
      temporalio workflow sandbox.
"""

import os

TASK_QUEUE = (
    os.getenv("TEMPORAL_TASK_QUEUE_CODING_TEAM", "coding_team-queue").strip() or "coding_team-queue"
)
WORKFLOW_ID_PREFIX = "coding_team-"
