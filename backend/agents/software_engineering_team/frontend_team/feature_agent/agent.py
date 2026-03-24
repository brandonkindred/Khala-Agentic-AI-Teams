"""Compatibility shim: re-exports from frontend_team_deprecated.feature_agent.agent."""

from frontend_team_deprecated.feature_agent.agent import *  # noqa: F401, F403
from frontend_team_deprecated.feature_agent.agent import (  # noqa: F401
    FrontendExpertAgent,
    _apply_frontend_build_fix_edits,
    _extract_affected_file_paths_from_frontend_build_errors,
    _read_frontend_affected_files_code,
    _read_repo_code,
)
