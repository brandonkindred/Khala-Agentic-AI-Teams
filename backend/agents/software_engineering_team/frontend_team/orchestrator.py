"""Compatibility shim: re-exports from frontend_team_deprecated.orchestrator."""

from frontend_team_deprecated.orchestrator import *  # noqa: F401, F403
from frontend_team_deprecated.orchestrator import (  # noqa: F401
    FrontendOrchestratorAgent,
    _is_lightweight_task,
)
