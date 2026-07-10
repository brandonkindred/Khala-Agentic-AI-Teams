"""Temporal task queue and workflow IDs for the Agent Provisioning team."""

import os

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "agent-provisioning").strip()
WORKFLOW_ID_PREFIX = "agent-provisioning-"

# --- Sandbox lifecycle workflows -------------------------------------------
# Activity timeouts (seconds). The acquire ceiling must exceed the sandbox boot
# timeout (AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S, default 90) so a cold
# start isn't killed mid-provision.
SANDBOX_ACQUIRE_TIMEOUT_S = int(os.getenv("AGENT_PROVISIONING_SANDBOX_ACQUIRE_TIMEOUT_S", "300"))
SANDBOX_TEARDOWN_TIMEOUT_S = int(os.getenv("AGENT_PROVISIONING_SANDBOX_TEARDOWN_TIMEOUT_S", "120"))
SANDBOX_REAP_TIMEOUT_S = int(os.getenv("AGENT_PROVISIONING_SANDBOX_REAP_TIMEOUT_S", "300"))

# The idle-reaper is a single self-scheduling workflow. Fixed id → at most one
# instance runs; a duplicate start is a no-op (WorkflowAlreadyStartedError).
SANDBOX_REAPER_INTERVAL_S = int(os.getenv("AGENT_PROVISIONING_SANDBOX_REAPER_INTERVAL_S", "60"))
SANDBOX_REAPER_WORKFLOW_ID = "agent-provisioning-sandbox-idle-reaper"
