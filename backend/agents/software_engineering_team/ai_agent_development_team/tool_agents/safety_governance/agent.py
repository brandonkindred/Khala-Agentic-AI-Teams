"""Safety and governance tool agent."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import get_strands_model

from ...models import ToolAgentInput, ToolAgentOutput

PROMPT = """You are an expert AI safety and governance specialist.
Generate policy guards, approval gates, and risk controls.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class SafetyGovernanceToolAgent:
    def __init__(self, llm=None) -> None:
        self._model = get_strands_model()

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        raw = json.loads((lambda _r: _r.message if hasattr(_r, "message") else str(_r))(Agent(model=self._model)(
            PROMPT.format(
                microtask=inp.microtask.description or inp.microtask.title,
                spec=inp.spec_context[:5000],
            )).strip()),
            think=True,
        )
        return ToolAgentOutput(
            files=raw.get("files") or {},
            recommendations=raw.get("recommendations") or [],
            summary=raw.get("summary", ""),
        )
