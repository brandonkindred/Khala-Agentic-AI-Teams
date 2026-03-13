"""Temporal task queue and workflow IDs for the Nutrition & Meal Planning team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_NUTRITION", "nutrition-meal-planning").strip()
WORKFLOW_ID_PREFIX = "nutrition-meal-planning-"
