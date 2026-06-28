"""
Git branch management tool agent for backend-code-v2.

The implementation is shared, team-agnostic, and lives in
:mod:`software_engineering_team.shared.tool_agent_git_branch`; this module
re-exports it so existing import paths keep working.
"""

from __future__ import annotations

from software_engineering_team.shared.tool_agent_git_branch import (  # noqa: F401
    GitBranchManagementToolAgent,
    _FilesPayload,
)

__all__ = ["GitBranchManagementToolAgent"]
