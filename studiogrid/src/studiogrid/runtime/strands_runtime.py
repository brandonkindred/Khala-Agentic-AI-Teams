from __future__ import annotations

import json
from typing import Any

from studiogrid.runtime.errors import SchemaValidationError

try:
    from strands import Agent
except Exception:  # pragma: no cover - fallback for dev envs without strands sdk
    class Agent:  # type: ignore[no-redef]
        def __init__(self, tools: list[Any] | None = None) -> None:
            self.tools = tools or []

        def __call__(self, prompt: str) -> str:
            return json.dumps(
                {
                    "kind": "ARTIFACT",
                    "payload": {
                        "artifact_type": "draft",
                        "format": "json",
                        "payload": {"prompt_preview": prompt[:60]},
                    },
                }
            )


class StrandsAgentExecutor:
    def __init__(self, registry, tool_factory):
        self.registry = registry
        self.tool_factory = tool_factory

    def run(self, *, agent_id: str, task_envelope: dict[str, Any]) -> dict[str, Any]:
        agent_cfg = self.registry.get_agent(agent_id)
        tools = self.tool_factory.build_tools(agent_cfg.get("tools", []), agent_cfg.get("permissions", []))
        agent = Agent(tools=tools)
        prompt = self._build_prompt(agent_cfg, task_envelope)
        result_text = agent(prompt)
        try:
            return json.loads(result_text)
        except Exception as exc:
            raise SchemaValidationError(f"Agent {agent_id} did not output valid JSON: {exc}")

    def _build_prompt(self, agent_cfg: dict[str, Any], task_envelope: dict[str, Any]) -> str:
        prompt_text = self.registry.load_prompt(agent_cfg["prompt_file"])
        return f"{prompt_text}\n\nTASK_ENVELOPE_JSON:\n{json.dumps(task_envelope, indent=2)}\n"
