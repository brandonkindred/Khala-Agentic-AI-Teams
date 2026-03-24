"""Runtime integration tool agent."""

from __future__ import annotations

from llm_service import LLMClient

from ...models import ToolAgentInput, ToolAgentOutput

PROMPT = """You are an agent runtime integration specialist.
Design orchestrator runtime wiring, tool contracts, retries, and observability hooks.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class AgentRuntimeToolAgent:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        raw = self.llm.complete_json(
            PROMPT.format(
                microtask=inp.microtask.description or inp.microtask.title,
                spec=inp.spec_context[:5000],
            )
        )
        return ToolAgentOutput(
            files=raw.get("files") or {},
            recommendations=raw.get("recommendations") or [],
            summary=raw.get("summary", ""),
        )
