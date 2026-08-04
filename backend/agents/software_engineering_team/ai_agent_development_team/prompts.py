"""Prompt templates for AI Agent Development Team phases."""

from __future__ import annotations

from .constants import REQUIRED_ARTIFACT_HINTS

DELIVER_PROMPT = """You are an expert delivery coordinator.
Given generated artifacts and review findings, produce final delivery notes.
Respond JSON:
{
  "summary": "...",
  "handoff_notes": ["..."],
  "runbook": ["..."]
}
"""


def _required_artifact_hints_line() -> str:
    """Format the shared hint list for injection into system prompts.

    Preconditions: ``REQUIRED_ARTIFACT_HINTS`` is a non-empty sequence of strings.
    Postconditions: returns one line that includes every hint joined by ``", "``.
    """
    joined = ", ".join(REQUIRED_ARTIFACT_HINTS)
    return (
        "Required artifact path categories (each must appear in at least one "
        f"generated artifact filename later): {joined}."
    )


def intake_system_prompt() -> str:
    """Build the intake specialist system prompt with shared artifact hints.

    Preconditions: none beyond importable ``REQUIRED_ARTIFACT_HINTS``.
    Postconditions: returns a system prompt that retains the intake JSON schema
      and includes every entry of ``REQUIRED_ARTIFACT_HINTS``.
    """
    return f"""You are an expert spec intake specialist for building AI agent systems.
Extract a normalized mission brief from the task and spec.
{_required_artifact_hints_line()}
Respond with JSON:
{{
  "system_goal": "...",
  "constraints": ["..."],
  "risks": ["..."],
  "success_metrics": ["..."],
  "summary": "..."
}}
"""


def planning_system_prompt() -> str:
    """Build the planning specialist system prompt with shared artifact hints.

    Preconditions: none beyond importable ``REQUIRED_ARTIFACT_HINTS``.
    Postconditions: returns a system prompt that retains the planning JSON schema,
      tool-agent list, and includes every entry of ``REQUIRED_ARTIFACT_HINTS``.
    """
    return f"""You are an AI systems planner.
Create microtasks to deliver a production-ready agent system blueprint.
Use available tool agents: prompt_engineering, memory_rag, safety_governance, evaluation_harness, agent_runtime, mcp_server_connectivity, general.
{_required_artifact_hints_line()}
Respond with JSON:
{{
  "microtasks": [{{"id":"mt-1","title":"...","description":"...","tool_agent":"prompt_engineering","depends_on":[]}}],
  "summary": "..."
}}
"""
