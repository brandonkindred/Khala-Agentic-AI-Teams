"""Tests that the read-only repo-inspection tools register with Strands and reach the SWE agent.

The Strands registry silently drops unrecognized tool specs, so a misbuilt tool would
leave the agent running without it. These tests pin that the inspection definitions become
registrable ``AgentTool``s carrying their schema verbatim, and that ``run_implement`` wires
them alongside the git tools into the model's toolset.
"""

from __future__ import annotations

from typing import Any, Dict

from strands.tools.registry import ToolRegistry
from strands.types.tools import AgentTool

from agent_repo_tools import REPO_INSPECT_TOOL_DEFINITIONS, build_repo_inspect_handlers
from coding_team.models import StackSpec, Task
from coding_team.senior_software_engineer_agent import agent as swe_mod
from coding_team.senior_software_engineer_agent.agent import SeniorSWEAgent, _build_strands_tools

_NAMES = ["list_files", "read_file"]


def test_repo_inspect_tools_register_with_strands_registry(tmp_path) -> None:
    handlers = build_repo_inspect_handlers(tmp_path)
    tools = _build_strands_tools(handlers, REPO_INSPECT_TOOL_DEFINITIONS)
    assert len(tools) == len(REPO_INSPECT_TOOL_DEFINITIONS)
    assert all(isinstance(t, AgentTool) for t in tools)
    registered = ToolRegistry().process_tools(tools)
    assert sorted(registered) == sorted(_NAMES)


def test_repo_inspect_tool_spec_carries_definition_schema_verbatim(tmp_path) -> None:
    handlers = build_repo_inspect_handlers(tmp_path)
    by_name = {
        t.tool_name: t for t in _build_strands_tools(handlers, REPO_INSPECT_TOOL_DEFINITIONS)
    }
    for definition in REPO_INSPECT_TOOL_DEFINITIONS:
        fn = definition["function"]
        spec = by_name[fn["name"]].tool_spec
        assert spec["name"] == fn["name"]
        assert spec["description"] == fn["description"]
        assert spec["inputSchema"] == {"json": fn["parameters"]}


def test_run_implement_wires_inspection_tools_into_agent(tmp_path, monkeypatch) -> None:
    captured: Dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured["tools"] = kw.get("tools", [])

        def __call__(self, prompt):
            return '{"summary":"ok","files_to_create_or_edit":[],"commands_run":[],"ready_for_review":true}'

    # The git tools need a real .git dir to build their context cleanly; the inspection
    # tools only need the workspace path.
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)

    swe = SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    swe.run_implement(Task(id="t1", title="T1", description="do it"), tmp_path, use_git_tools=True)

    tool_names = {t.tool_name for t in captured["tools"]}
    assert {"list_files", "read_file"}.issubset(tool_names)


def test_run_implement_prompt_mentions_inspection_tools(tmp_path, monkeypatch) -> None:
    captured: Dict[str, Any] = {}

    class FakeAgent:
        def __init__(self, **kw):
            captured["system"] = kw.get("system_prompt", "")

        def __call__(self, prompt):
            return '{"summary":"ok","files_to_create_or_edit":[],"commands_run":[],"ready_for_review":true}'

    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(swe_mod, "Agent", FakeAgent)

    swe = SeniorSWEAgent(agent_id="a1", stack_spec=StackSpec(name="backend"), llm=object())
    swe.run_implement(Task(id="t1", title="T1", description="do it"), tmp_path, use_git_tools=True)

    assert "list_files" in captured["system"]
    assert "read_file" in captured["system"]
