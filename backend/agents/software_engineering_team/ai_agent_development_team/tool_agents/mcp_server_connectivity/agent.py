"""MCP server discovery, setup, and connectivity tool agent."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from .._base import JsonGeneratorToolAgent

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


class MCPServerConnectivityToolAgent(JsonGeneratorToolAgent):
    """Generates MCP discovery/setup/connectivity artifacts for AI agent systems."""

    PROMPT = PROMPT
