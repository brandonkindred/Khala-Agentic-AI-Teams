"""
Tests for the Senior SWE agent's git-tool bridge into the Strands SDK.

The Strands tool registry only registers recognized tool types (``AgentTool``
instances, ``@tool``-decorated functions, modules, ...). A plain closure is
dropped with an "unrecognized tool specification" warning and the agent
silently runs without any git tools. These tests pin the contract of
``_build_strands_tools``: every git tool definition must become a registrable
``AgentTool`` that carries the definition's exact JSON schema and round-trips
invocations to the matching handler.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from strands.tools.registry import ToolRegistry
from strands.types.tools import AgentTool, ToolResult, ToolUse

from agent_git_tools import GIT_TOOL_DEFINITIONS
from coding_team.senior_software_engineer_agent.agent import _build_strands_tools

_ALL_NAMES = [d["function"]["name"] for d in GIT_TOOL_DEFINITIONS]


def _handlers(fn: Callable[[str], Callable[[dict], Any]] | None = None) -> dict[str, Any]:
    """Build a full name->handler map; ``fn(name)`` supplies each handler."""
    make = fn or (lambda name: lambda args: {"handler": name})
    return {name: make(name) for name in _ALL_NAMES}


def _invoke(tool: AgentTool, tool_use: ToolUse) -> ToolResult:
    """Drive a tool through its public ``stream`` API and return the ToolResult."""

    async def run() -> ToolResult:
        events = [event async for event in tool.stream(tool_use, {})]
        return events[-1].tool_result

    return asyncio.run(run())


def test_all_git_tools_register_with_strands_registry() -> None:
    """Every definition with a handler must register — none may be dropped
    with the registry's "unrecognized tool specification" warning."""
    tools = _build_strands_tools(_handlers(), GIT_TOOL_DEFINITIONS)
    assert len(tools) == len(GIT_TOOL_DEFINITIONS)
    registered = ToolRegistry().process_tools(tools)
    assert sorted(registered) == sorted(_ALL_NAMES)


def test_tools_are_agent_tool_instances() -> None:
    """The registry's recognized branch is ``isinstance(tool, AgentTool)``."""
    tools = _build_strands_tools(_handlers(), GIT_TOOL_DEFINITIONS)
    assert all(isinstance(t, AgentTool) for t in tools)


def test_tool_spec_carries_definition_schema_verbatim() -> None:
    """The LLM must see the same name/description/parameters the OpenAI-style
    definitions declare — not a schema derived from a ``**kwargs`` signature."""
    tools = _build_strands_tools(_handlers(), GIT_TOOL_DEFINITIONS)
    by_name = {t.tool_name: t for t in tools}
    for definition in GIT_TOOL_DEFINITIONS:
        fn = definition["function"]
        spec = by_name[fn["name"]].tool_spec
        assert spec["name"] == fn["name"]
        assert spec["description"] == fn["description"]
        assert spec["inputSchema"] == {"json": fn["parameters"]}


def test_invocation_dispatches_to_named_handler_with_input() -> None:
    calls: dict[str, dict] = {}

    def make(name: str) -> Callable[[dict], Any]:
        def handler(args: dict) -> dict:
            calls[name] = args
            return {"handler": name, "args": args}

        return handler

    tools = _build_strands_tools(_handlers(make), GIT_TOOL_DEFINITIONS)
    diff = next(t for t in tools if t.tool_name == "git_diff")
    result = _invoke(diff, {"toolUseId": "tu-1", "name": "git_diff", "input": {"staged": True}})
    assert calls == {"git_diff": {"staged": True}}
    assert result["toolUseId"] == "tu-1"
    assert result["status"] == "success"
    assert json.loads(result["content"][0]["text"]) == {
        "handler": "git_diff",
        "args": {"staged": True},
    }


def test_missing_input_defaults_to_empty_args() -> None:
    """A provider sending ``input: null`` must not crash the handler."""
    calls: dict[str, dict] = {}

    def make(name: str) -> Callable[[dict], Any]:
        def handler(args: dict) -> dict:
            calls[name] = args
            return {}

        return handler

    tools = _build_strands_tools(_handlers(make), GIT_TOOL_DEFINITIONS)
    status = next(t for t in tools if t.tool_name == "git_status")
    result = _invoke(status, {"toolUseId": "tu-2", "name": "git_status", "input": None})
    assert calls == {"git_status": {}}
    assert result["status"] == "success"


def test_string_handler_result_passes_through_unencoded() -> None:
    tools = _build_strands_tools(
        _handlers(lambda name: lambda args: "plain text"), GIT_TOOL_DEFINITIONS
    )
    result = _invoke(tools[0], {"toolUseId": "tu-3", "name": tools[0].tool_name, "input": {}})
    assert result["status"] == "success"
    assert result["content"][0]["text"] == "plain text"


def test_handler_exception_becomes_error_result() -> None:
    """A failing git op must surface to the model as an error ToolResult,
    not abort the whole agent invocation."""

    def boom(args: dict) -> dict:
        raise RuntimeError("boom")

    tools = _build_strands_tools(_handlers(lambda name: boom), GIT_TOOL_DEFINITIONS)
    result = _invoke(tools[0], {"toolUseId": "tu-9", "name": tools[0].tool_name, "input": {}})
    assert result["toolUseId"] == "tu-9"
    assert result["status"] == "error"
    assert "boom" in result["content"][0]["text"]


def test_skips_definitions_without_handlers() -> None:
    handlers = {"git_status": lambda args: {}}
    tools = _build_strands_tools(handlers, GIT_TOOL_DEFINITIONS)
    assert [t.tool_name for t in tools] == ["git_status"]
