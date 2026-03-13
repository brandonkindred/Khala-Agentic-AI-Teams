"""Temporal task queue and workflow IDs for the Agent Provisioning team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "agent-provisioning").strip()
WORKFLOW_ID_PREFIX = "agent-provisioning-"
