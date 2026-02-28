from __future__ import annotations

import json
from importlib import import_module
from typing import Any

from studiogrid.runtime.errors import SchemaValidationError


class StrandsAgentExecutor:
    """Runs a configured Strands agent and enforces single-envelope JSON output."""

    def __init__(self, registry, tool_factory):
        self.registry = registry
        self.tool_factory = tool_factory

    def _agent_class(self):
        module = import_module("strands")
        return module.Agent

    def run(self, *, agent_id: str, task_envelope: dict[str, Any]) -> dict[str, Any]:
        agent_cfg = self.registry.get_agent(agent_id)
        tools = self.tool_factory.build_tools(agent_cfg.get("tools", []), agent_cfg.get("permissions", []))
        agent_cls = self._agent_class()
        agent = agent_cls(tools=tools)
        prompt = self._build_prompt(agent_cfg, task_envelope)
        result_text = agent(prompt)
        try:
            return json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise SchemaValidationError(f"Agent {agent_id} did not output valid JSON: {exc}")

    def _build_prompt(self, agent_cfg: dict[str, Any], task_envelope: dict[str, Any]) -> str:
        prompt_text = self.registry.load_prompt(agent_cfg["prompt_file"])
        return f"{prompt_text}\n\nTASK_ENVELOPE_JSON:\n{json.dumps(task_envelope, indent=2)}\n"
