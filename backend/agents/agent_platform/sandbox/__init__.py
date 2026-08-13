"""Agent-keyed sandbox lifecycle for ``agent_platform.sandbox``.

Runs the unified ``khala-agent-sandbox`` image as one ephemeral, hardened
container per specialist agent under test. Unified-API sandbox routes and
the invoke proxy import this package.
"""

from .lifecycle import (
    DockerUnavailableError,
    Lifecycle,
    SandboxAcquireFailedError,
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
    "DockerUnavailableError",
    "Lifecycle",
    "ReaperStats",
    "SandboxAcquireFailedError",
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
