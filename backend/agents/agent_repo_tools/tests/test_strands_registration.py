"""The inspection tool definitions must register cleanly with the Strands SDK.

The Strands registry silently drops unrecognized tool specs, so a misbuilt definition
would leave an agent running without the tool. These tests pin that the inspection
definitions become registrable ``AgentTool``s carrying their schema verbatim, using the
same bridge (``_build_strands_tools``) the Senior SWE agent uses. They live here — beside
the definitions they guard — so they run in this module's CI job.
"""

from __future__ import annotations

from strands.tools.registry import ToolRegistry
from strands.types.tools import AgentTool

from agent_repo_tools import REPO_INSPECT_TOOL_DEFINITIONS, build_repo_inspect_handlers
from coding_team.senior_software_engineer_agent.agent import _build_strands_tools

_NAMES = ["list_files", "read_file"]


def test_repo_inspect_tools_register_with_strands_registry(tmp_path) -> None:
    tools = _build_strands_tools(
        build_repo_inspect_handlers(tmp_path), REPO_INSPECT_TOOL_DEFINITIONS
    )
    assert len(tools) == len(REPO_INSPECT_TOOL_DEFINITIONS)
    assert all(isinstance(t, AgentTool) for t in tools)
    assert sorted(ToolRegistry().process_tools(tools)) == sorted(_NAMES)


def test_repo_inspect_tool_spec_carries_definition_schema_verbatim(tmp_path) -> None:
    tools = _build_strands_tools(
        build_repo_inspect_handlers(tmp_path), REPO_INSPECT_TOOL_DEFINITIONS
    )
    by_name = {t.tool_name: t for t in tools}
    for definition in REPO_INSPECT_TOOL_DEFINITIONS:
        fn = definition["function"]
        spec = by_name[fn["name"]].tool_spec
        assert spec["name"] == fn["name"]
        assert spec["description"] == fn["description"]
        assert spec["inputSchema"] == {"json": fn["parameters"]}
