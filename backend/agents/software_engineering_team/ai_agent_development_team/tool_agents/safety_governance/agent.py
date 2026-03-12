"""Safety and governance tool agent."""

from __future__ import annotations

from llm_service import LLMClient

from ...models import ToolAgentInput, ToolAgentOutput

PROMPT = """You are an expert AI safety and governance specialist.
Generate policy guards, approval gates, and risk controls.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class SafetyGovernanceToolAgent:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        raw = self.llm.complete_json(
            PROMPT.format(microtask=inp.microtask.description or inp.microtask.title, spec=inp.spec_context[:5000])
        )
        return ToolAgentOutput(
            files=raw.get("files") or {}, recommendations=raw.get("recommendations") or [], summary=raw.get("summary", "")
        )
