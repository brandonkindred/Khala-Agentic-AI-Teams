"""Memory and RAG design tool agent."""

from __future__ import annotations

from strands import Agent  # noqa: F401  (kept so tests can monkeypatch this module's Agent)

from .._base import JsonGeneratorToolAgent

PROMPT = """You are an expert memory/RAG specialist.
Design retrieval index strategy, memory layers, and context assembly contracts.
Microtask: {microtask}
Spec context: {spec}
Return JSON with files/recommendations/summary.
"""


class MemoryRagToolAgent(JsonGeneratorToolAgent):
    """Designs retrieval index strategy, memory layers, and context-assembly contracts."""

    PROMPT = PROMPT
