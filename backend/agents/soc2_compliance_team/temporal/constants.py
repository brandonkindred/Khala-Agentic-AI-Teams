"""Temporal task queue and workflow IDs for the SOC2 compliance team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_SOC2", "soc2-compliance").strip()
WORKFLOW_ID_PREFIX_AUDIT = "soc2-audit-"
