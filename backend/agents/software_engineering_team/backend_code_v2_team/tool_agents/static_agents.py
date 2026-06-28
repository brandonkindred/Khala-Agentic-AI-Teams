"""
Backward-compat re-export of the code-v2 "static phase" tool-agent bases.

The implementations now live, team-agnostically, in
:mod:`software_engineering_team.shared.tool_agent_static`. This module remains so
existing ``from ..static_agents import …`` importers (auth, api_openapi,
data_engineering, cicd, containerization) keep working unchanged.

``FileGeneratorToolAgent`` resolves its template parser from the
``_parse_files_and_summary`` class-attribute hook the concrete subclass sets
(the team-specific ``parse_files_and_summary_template``).
"""

from __future__ import annotations

from software_engineering_team.shared.tool_agent_static import (  # noqa: F401
    MAX_EXISTING_CODE_CHARS,
    FileGeneratorToolAgent,
    StaticPhaseToolAgent,
    StubToolAgent,
)

__all__ = [
    "MAX_EXISTING_CODE_CHARS",
    "FileGeneratorToolAgent",
    "StaticPhaseToolAgent",
    "StubToolAgent",
]
