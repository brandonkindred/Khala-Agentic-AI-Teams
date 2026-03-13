"""Temporal task queue and workflow IDs for the Planning V3 team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_PLANNING_V3", "planning-v3").strip()
WORKFLOW_ID_PREFIX = "planning-v3-"
