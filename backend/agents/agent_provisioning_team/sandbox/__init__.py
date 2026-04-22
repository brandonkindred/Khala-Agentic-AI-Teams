"""Agent-keyed sandbox lifecycle for ``agent_provisioning_team`` (issue #264).

Runs the unified ``khala-agent-sandbox`` image from Phase 1 (#263) as one
ephemeral, hardened container per specialist agent under test. The unified
API and Agent Console switch to this module in Phase 3 (#265).
"""

from .lifecycle import Lifecycle, UnknownAgentError
from .state import (
    SandboxHandle,
    SandboxState,
    SandboxStatus,
    state_file_path,
)

__all__ = [
    "Lifecycle",
    "SandboxHandle",
    "SandboxState",
    "SandboxStatus",
    "UnknownAgentError",
    "state_file_path",
]
