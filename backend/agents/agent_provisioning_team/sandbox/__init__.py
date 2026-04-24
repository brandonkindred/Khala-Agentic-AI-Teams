"""Agent-keyed sandbox lifecycle for ``agent_provisioning_team`` (issue #264).

Runs the unified ``khala-agent-sandbox`` image from Phase 1 (#263) as one
ephemeral, hardened container per specialist agent under test. The unified
API and Agent Console switch to this module in Phase 3 (#265).
"""

from .lifecycle import (
    Lifecycle,
    UnknownAgentError,
    acquire,
    get_lifecycle,
    list_active,
    metrics,
    note_activity,
    run_idle_reaper,
    status,
    teardown,
)
from .state import (
    AgeStats,
    BootMsStats,
    ReaperStats,
    SandboxHandle,
    SandboxMetrics,
    SandboxState,
    SandboxStatus,
    state_file_path,
)

__all__ = [
    "AgeStats",
    "BootMsStats",
    "Lifecycle",
    "ReaperStats",
    "SandboxHandle",
    "SandboxMetrics",
    "SandboxState",
    "SandboxStatus",
    "UnknownAgentError",
    "acquire",
    "get_lifecycle",
    "list_active",
    "metrics",
    "note_activity",
    "run_idle_reaper",
    "state_file_path",
    "status",
    "teardown",
]
