"""Bridge OpenAI-style tool definitions into Strands AgentTool instances."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from strands.tools.tools import PythonAgentTool
from strands.types.tools import ToolResult, ToolSpec, ToolUse

logger = logging.getLogger(__name__)


def _make_python_agent_tool(
    name: str,
    handler: Any,
    description: str,
    parameters: dict,
) -> PythonAgentTool:
    """Wrap one tool handler in a Strands ``PythonAgentTool``."""
    spec: ToolSpec = {
        "name": name,
        "description": description or name,
        "inputSchema": {"json": parameters},
    }

    def tool_func(tool_use: ToolUse, **_invocation_state: Any) -> ToolResult:
        tool_use_id = tool_use.get("toolUseId", "")
        try:
            tool_input = tool_use.get("input")
            out = handler(tool_input if tool_input is not None else {})
        except Exception as exc:  # noqa: BLE001 - tool failures should reach the model
            return {
                "toolUseId": tool_use_id,
                "status": "error",
                "content": [{"text": f"{type(exc).__name__}: {exc}"}],
            }
        text = out if isinstance(out, str) else json.dumps(out)
        return {
            "toolUseId": tool_use_id,
            "status": "success",
            "content": [{"text": text}],
        }

    return PythonAgentTool(name, spec, tool_func)


def build_strands_tools(
    handlers: Dict[str, Any],
    tool_definitions: List[Dict[str, Any]],
) -> List[PythonAgentTool]:
    """Convert OpenAI-style tool definitions plus handlers into Strands tools."""
    tools: List[PythonAgentTool] = []
    for tool_def in tool_definitions:
        func_info = tool_def.get("function", {})
        name = func_info.get("name")
        if name and name in handlers:
            tools.append(
                _make_python_agent_tool(
                    name,
                    handlers[name],
                    func_info.get("description", ""),
                    func_info.get("parameters", {}),
                )
            )
        elif name:
            logger.debug("No handler for tool %s, skipping", name)
    return tools
