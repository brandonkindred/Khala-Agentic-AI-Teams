"""MCP server discovery, setup, and connectivity tool agent."""

from __future__ import annotations

from software_engineering_team.shared.llm import LLMClient

from ...models import ToolAgentInput, ToolAgentOutput

PROMPT = """You are an MCP integration specialist for agent systems.
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

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def run(self, inp: ToolAgentInput) -> ToolAgentOutput:
        raw = self.llm.complete_json(
            PROMPT.format(microtask=inp.microtask.description or inp.microtask.title, spec=inp.spec_context[:5000])
        )
        return ToolAgentOutput(
            files=raw.get("files") or {},
            recommendations=raw.get("recommendations") or [],
            summary=raw.get("summary", ""),
        )
