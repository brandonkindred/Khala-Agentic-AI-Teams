"""Temporal task queue and timeouts for the platform sandbox worker.

Preconditions:
    * Imported only by sandbox Temporal modules and their tests.
Postconditions:
    * Queue name, workflow ids, and env-var names match the pre-move values so
      in-flight Temporal identity does not change.
"""

import os

from shared.env_config import env_int

_PROVISIONING_QUEUE_DEFAULT = os.getenv(
    "TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING", "agent-provisioning"
).strip()

SANDBOX_TASK_QUEUE = os.getenv(
    "TEMPORAL_TASK_QUEUE_AGENT_PROVISIONING_SANDBOX",
    f"{_PROVISIONING_QUEUE_DEFAULT}-sandbox",
).strip()

SANDBOX_WORKFLOW_ID_PREFIX = "agent-provisioning-"

SANDBOX_ACQUIRE_TIMEOUT_S = env_int("AGENT_PROVISIONING_SANDBOX_ACQUIRE_TIMEOUT_S", 300, floor=1)
SANDBOX_TEARDOWN_TIMEOUT_S = env_int("AGENT_PROVISIONING_SANDBOX_TEARDOWN_TIMEOUT_S", 120, floor=1)
SANDBOX_REAP_TIMEOUT_S = env_int("AGENT_PROVISIONING_SANDBOX_REAP_TIMEOUT_S", 300, floor=1)
SANDBOX_REAPER_INTERVAL_S = env_int("AGENT_PROVISIONING_SANDBOX_REAPER_INTERVAL_S", 60, floor=1)
SANDBOX_REAPER_WORKFLOW_ID = "agent-provisioning-sandbox-idle-reaper"

CLIENT_TIMEOUT_MARGIN_S = 120

SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_SANDBOX_ACQUIRE_CLIENT_TIMEOUT_S",
    SANDBOX_ACQUIRE_TIMEOUT_S * 3 + CLIENT_TIMEOUT_MARGIN_S,
    floor=1,
)
SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S = env_int(
    "AGENT_PROVISIONING_SANDBOX_TEARDOWN_CLIENT_TIMEOUT_S",
    SANDBOX_TEARDOWN_TIMEOUT_S * 3 + CLIENT_TIMEOUT_MARGIN_S,
    floor=1,
)
