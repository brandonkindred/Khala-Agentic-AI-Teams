"""Temporal task queue and workflow IDs for the social media marketing team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_SOCIAL_MARKETING", "social-marketing").strip()
WORKFLOW_ID_PREFIX_RUN = "social-marketing-run-"
