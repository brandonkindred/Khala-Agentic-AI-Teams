"""Prompt engineering tool agent."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import get_strands_model
from llm_service.strands_model import resolve_strands_model

from ...models import ToolAgentInput, ToolAgentOutput

PROMPT = """You are an expert prompt engineering specialist for multi-agent systems.
Create prompt artifacts for this microtask.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class PromptEngineeringToolAgent:
    def __init__(self, llm=None) -> None:
        self._model = resolve_strands_model(llm, get_strands_model_fn=get_strands_model)

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        agent = Agent(model=self._model)
        prompt = PROMPT.format(
            microtask=inp.microtask.description or inp.microtask.title,
            spec=inp.spec_context[:5000],
        )
        raw = json.loads(str(agent(prompt)).strip())
        return ToolAgentOutput(
            files=raw.get("files") or {},
            recommendations=raw.get("recommendations") or [],
            summary=raw.get("summary", ""),
        )
