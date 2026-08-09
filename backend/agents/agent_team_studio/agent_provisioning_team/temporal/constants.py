"""Temporal task queue and workflow IDs for the Agent Provisioning team."""

import os

from shared.env_config import env_int

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "agent-provisioning").strip()
WORKFLOW_ID_PREFIX = "agent-provisioning-"

# Sandbox workflows/activities run on their OWN task queue, separate from
# TASK_QUEUE above, and are served by a worker booted only inside the unified
# API process (temporal/worker.py's
# start_agent_provisioning_sandbox_temporal_worker_thread), never by the
# standalone agent-provisioning-service team container that also runs a
# worker on TASK_QUEUE. Reason: sandbox/lifecycle.py's Lifecycle singleton is
# process-local in-memory state. If sandbox activities were on the shared
# TASK_QUEUE, Temporal could dispatch one to whichever process's worker picks
# it up first — including the standalone team-service container, whose
# Lifecycle singleton is a completely separate in-memory dict from the
# unified API's. status()/list_active()/metrics()/note_activity() always run
# directly (never via Temporal) against the unified API's own Lifecycle
# instance, so a sandbox mutation landing in the other process would silently
# diverge from what those reads observe — e.g. the reaper tearing down a
# sandbox it (wrongly) believes is idle, or metrics/list staying empty despite
# real activity. Pinning sandbox work to its own queue polled only by a
# worker started from unified_api/main.py's own lifespan keeps every sandbox
# mutation and the Lifecycle singleton it affects in the same process.
SANDBOX_TASK_QUEUE = os.getenv(
    "TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX", f"{TASK_QUEUE}-sandbox"
).strip()

# --- Sandbox lifecycle workflows -------------------------------------------
# Activity timeouts (seconds). All numeric vars here parse defensively via
# shared.env_config.env_int per CLAUDE.md ("garbage -> documented default,
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

# SandboxAcquireWorkflow: SANDBOX_ACQUIRE_TIMEOUT_S per attempt x up to 3
# attempts (SANDBOX_ACQUIRE_RETRY_POLICY), plus backoff, plus margin.
SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S",
    SANDBOX_ACQUIRE_TIMEOUT_S * 3 + CLIENT_TIMEOUT_MARGIN_S,
    floor=1,
)
# SandboxTeardownWorkflow: SANDBOX_TEARDOWN_TIMEOUT_S per attempt x up to 3
# attempts (SANDBOX_RETRY_POLICY), plus backoff, plus margin.
SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S",
    SANDBOX_TEARDOWN_TIMEOUT_S * 3 + CLIENT_TIMEOUT_MARGIN_S,
    floor=1,
)
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
