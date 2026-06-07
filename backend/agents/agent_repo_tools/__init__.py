"""Shared read-only repo-inspection tools for any repo-backed agent.

Gives an agent active inspection of its job workspace — list a directory, open a
file in full — sandboxed to the workspace root. These complement (do not replace)
any passive repo-context summary the host injects. All operations are read-only;
mutation lives in ``agent_git_tools``.
"""

from .context import RepoToolContext
from .definitions import REPO_INSPECT_TOOL_DEFINITIONS
from .executor import build_repo_inspect_handlers, execute_repo_tool

__all__ = [
    "RepoToolContext",
    "REPO_INSPECT_TOOL_DEFINITIONS",
    "build_repo_inspect_handlers",
    "execute_repo_tool",
]
