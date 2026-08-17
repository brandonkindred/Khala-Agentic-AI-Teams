"""Temporal task queue and workflow IDs for the Agent Provisioning team."""

import os

from shared.env_config import env_int

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "agent-provisioning").strip()
WORKFLOW_ID_PREFIX = "agent-provisioning-"

# --- Per-agent_id ownership lock (shared/agent_lock.py) ---------------------
# AgentProvisioningWorkflow/AgentDeprovisioningWorkflow each acquire this lock
# for their whole run so two workflows never process the same agent_id at
# once. LOCK_ACQUIRE_TIMEOUT_S bounds how long a workflow keeps retrying a
# busy lock before giving up; LOCK_TTL_S is AgentLockStore's own staleness
# backstop for a lock orphaned by a hard-terminated workflow (bypassing its
# `finally` release) — normal paths always release explicitly and never rely
# on it, since AgentProvisioningWorkflow renews after every phase.
#
# The floor exists because a TTL shorter than the worst gap between two
# renewals defeats the lock: a second workflow could reclaim the "expired"
# record while the first is still legitimately running. The worst such gap
# is the tool fan-out phase in temporal/workflows.py — TOOL_ACTIVITY_TIMEOUT
# (15 min, start_to_close per attempt) x TOOL_RETRY_POLICY.maximum_attempts
# (4) = 60 min of activity time, plus inter-attempt backoff (~2 min) = ~62
# min worst case. The 90-minute floor keeps a healthy margin above that;
# raise it if either constant above grows.
LOCK_ACQUIRE_TIMEOUT_S = env_int("AGENT_PROVISIONING_LOCK_ACQUIRE_TIMEOUT_S", 1800, floor=1)
LOCK_TTL_S = env_int("AGENT_PROVISIONING_LOCK_TTL_S", 7200, floor=5400)

# --- Execute-and-wait client-side ceilings -----------------------------------
# These must each exceed the corresponding workflow's own worst-case runtime
# (its activity timeout(s) x retry attempts + backoff), or a legitimately slow
# -but-eventually-successful run surfaces to the caller as a client-side
# timeout even though the workflow is still executing durably server-side.
# Flat margin (seconds) added on top of the worst-case retry budget, to cover
# Temporal server-side scheduling/backoff overhead between activity attempts
# that isn't itself part of any single activity's timeout.
CLIENT_TIMEOUT_MARGIN_S = 120

# AgentDeprovisioningWorkflow: schedule_to_close_timeout (PHASE_TIMEOUT, 20
# minutes in temporal/workflows.py) already caps the total time across
# DEFAULT_RETRY_POLICY's retries — once for deprovision_activity itself, and
# again for the release_agent_lock_activity that runs after it (also
# PHASE_TIMEOUT-bounded). The workflow also acquires the per-agent_id lock
# (LOCK_ACQUIRE_TIMEOUT_S) before deprovision_activity runs, so the
# client-side ceiling must budget for all three legs — acquire wait +
# deprovision + release — or it can time out and report a false failure while
# the workflow is still validly finishing up, plus margin.
DEPROVISION_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_DEPROVISION_CLIENT_TIMEOUT_S",
    LOCK_ACQUIRE_TIMEOUT_S + 20 * 60 + 20 * 60 + CLIENT_TIMEOUT_MARGIN_S,
    floor=1,
)

# Default in-container workspace path when setup did not persist one.
DEFAULT_WORKSPACE_PATH = (
    os.getenv("AGENT_PROVISIONING_DEFAULT_WORKSPACE_PATH", "/workspace").strip() or "/workspace"
)
