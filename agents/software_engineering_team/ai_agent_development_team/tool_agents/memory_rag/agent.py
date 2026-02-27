"""Memory and RAG design tool agent."""

from __future__ import annotations

from shared.llm import LLMClient

from ...models import ToolAgentInput, ToolAgentOutput

PROMPT = """You are a memory/RAG specialist.
Design retrieval index strategy, memory layers, and context assembly contracts.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class MemoryRagToolAgent:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        raw = self.llm.complete_json(
            PROMPT.format(microtask=inp.microtask.description or inp.microtask.title, spec=inp.spec_context[:5000])
        )
        return ToolAgentOutput(
            files=raw.get("files") or {}, recommendations=raw.get("recommendations") or [], summary=raw.get("summary", "")
        )
