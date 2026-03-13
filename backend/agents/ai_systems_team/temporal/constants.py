"""Temporal task queue and workflow IDs for the AI systems team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_AI_SYSTEMS", "ai-systems").strip()
WORKFLOW_ID_PREFIX_BUILD = "ai-systems-build-"
