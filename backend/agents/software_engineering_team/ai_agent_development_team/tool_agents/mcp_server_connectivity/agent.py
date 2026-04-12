"""MCP server discovery, setup, and connectivity tool agent."""

from __future__ import annotations

import json

from strands import Agent

from llm_service import get_strands_model

from ...models import ToolAgentInput, ToolAgentOutput

PROMPT = """You are an expert MCP integration specialist for agent systems.
Your responsibilities for this microtask:
1) Identify which MCP servers are needed by capability domain.
2) Produce setup/config artifacts (env vars, server registry entries, auth wiring, startup scripts).
3) Produce connectivity and health-check artifacts showing how agents should connect.
4) Document fallback behavior when MCP servers are unavailable.

Microtask: {microtask}
Spec context: {spec}

Return JSON with:
{{
  "files": {{"path/to/file": "content"}},
  "recommendations": ["..."],
  "summary": "..."
}}
"""


class MCPServerConnectivityToolAgent:
    """Generates MCP discovery/setup/connectivity artifacts for AI agent systems."""

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
