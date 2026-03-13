"""Temporal task queue and workflow IDs for the personal assistant team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_PA", "personal-assistant").strip()
WORKFLOW_ID_PREFIX_ASSISTANT = "pa-assistant-"
