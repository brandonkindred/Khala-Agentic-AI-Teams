"""Temporal task queue and workflow IDs for the Agent Provisioning team."""

import os

from shared_env_config import env_int

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "agent-provisioning").strip()
WORKFLOW_ID_PREFIX = "agent-provisioning-"

# --- Sandbox lifecycle workflows -------------------------------------------
# Activity timeouts (seconds). All numeric vars here parse defensively via
# shared_env_config.env_int per CLAUDE.md ("garbage -> documented default,
# out-of-range -> clamped to floor/ceiling") rather than a raw int(os.getenv())
# that would raise ValueError (crashing this module's import) on a malformed
# value. The acquire ceiling must exceed the sandbox boot timeout
# (AGENT_PROVISIONING_SANDBOX_BOOT_TIMEOUT_S, default 90) so a cold start isn't
# killed mid-provision.
SANDBOX_ACQUIRE_TIMEOUT_S = env_int("AGENT_PROVISIONING_SANDBOX_ACQUIRE_TIMEOUT_S", 300, floor=1)
SANDBOX_TEARDOWN_TIMEOUT_S = env_int("AGENT_PROVISIONING_SANDBOX_TEARDOWN_TIMEOUT_S", 120, floor=1)
SANDBOX_REAP_TIMEOUT_S = env_int("AGENT_PROVISIONING_SANDBOX_REAP_TIMEOUT_S", 300, floor=1)

# The idle-reaper is a single self-scheduling workflow. Fixed id → at most one
# instance runs; a duplicate start is a no-op (WorkflowAlreadyStartedError).
# floor=1 matches SandboxReaperWorkflow.run's own `assert interval_s > 0`.
SANDBOX_REAPER_INTERVAL_S = env_int("AGENT_PROVISIONING_SANDBOX_REAPER_INTERVAL_S", 60, floor=1)
SANDBOX_REAPER_WORKFLOW_ID = "agent-provisioning-sandbox-idle-reaper"

# --- Execute-and-wait client-side ceilings -----------------------------------
# These must each exceed the corresponding workflow's own worst-case runtime
# (its activity timeout(s) x retry attempts + backoff), or a legitimately slow
# -but-eventually-successful run surfaces to the caller as a client-side
# timeout even though the workflow is still executing durably server-side.
# SandboxAcquireWorkflow: SANDBOX_ACQUIRE_TIMEOUT_S per attempt x up to 3
# attempts (SANDBOX_ACQUIRE_RETRY_POLICY), plus backoff, plus margin.
SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S",
    SANDBOX_ACQUIRE_TIMEOUT_S * 3 + 120,
    floor=1,
)
# SandboxTeardownWorkflow: SANDBOX_TEARDOWN_TIMEOUT_S per attempt x up to 3
# attempts (SANDBOX_RETRY_POLICY), plus backoff, plus margin.
SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S",
    SANDBOX_TEARDOWN_TIMEOUT_S * 3 + 120,
    floor=1,
)
# AgentDeprovisioningWorkflow: schedule_to_close_timeout (PHASE_TIMEOUT, 20
# minutes in temporal/workflows.py) already caps the total time across
# DEFAULT_RETRY_POLICY's retries, so the client only needs that ceiling plus
# margin.
DEPROVISION_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_DEPROVISION_CLIENT_TIMEOUT_S", 20 * 60 + 120, floor=1
)
