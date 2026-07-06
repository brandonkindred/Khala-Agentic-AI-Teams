"""Temporal task queue and workflow IDs for the Planning team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_PLANNING", "planning").strip()
WORKFLOW_ID_PREFIX = "planning-"
