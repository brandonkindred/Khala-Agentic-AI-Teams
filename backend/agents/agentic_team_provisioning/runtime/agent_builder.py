"""Compile an AgenticTeamAgent roster definition into a live strands.Agent.

Used by the interactive testing mode to turn declarative agent
definitions (role, skills, capabilities, tools, expertise) into
runnable agents that can respond to user messages.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Optional strands import — graceful degradation when not installed.
try:
    from strands import Agent as StrandsAgent

    HAS_STRANDS = True
except ImportError:
    HAS_STRANDS = False
    StrandsAgent = None  # type: ignore[assignment,misc]

# Optional common tools from strands_tools.
_COMMON_TOOLS: list[Any] = []
try:
    from strands_tools import (  # type: ignore[import-untyped]
        current_time,
        http_request,
        python_repl,
    )

    _COMMON_TOOLS = [http_request, python_repl, current_time]
except ImportError:
    pass

# Registry mapping tool name strings from the roster to actual tool objects.
TOOL_REGISTRY: dict[str, Any] = {}
if len(_COMMON_TOOLS) >= 3:
    TOOL_REGISTRY.update(
        {
            "http_request": _COMMON_TOOLS[0],
            "http": _COMMON_TOOLS[0],
            "python_repl": _COMMON_TOOLS[1],
            "python": _COMMON_TOOLS[1],
            "current_time": _COMMON_TOOLS[2],
        }
    )


def build_system_prompt(
    agent_name: str,
    role: str,
    skills: list[str],
    capabilities: list[str],
    tools: list[str],
    expertise: list[str],
) -> str:
    """Construct a system prompt from the roster agent's metadata."""
    parts = [f"You are {agent_name}, a specialist agent."]
    parts.append(f"\nRole: {role}")
    if skills:
        parts.append(f"\nSkills: {', '.join(skills)}")
    if capabilities:
        parts.append(f"\nCapabilities: {', '.join(capabilities)}")
    if expertise:
        parts.append(f"\nExpertise: {', '.join(expertise)}")
    if tools:
        parts.append(f"\nAvailable tools: {', '.join(tools)}")
    parts.append(
        "\n\nRespond helpfully and concisely. Use your specialized knowledge "
        "to provide high-quality, actionable answers."
    )
    return "\n".join(parts)


def resolve_tools(tool_names: list[str]) -> list[Any]:
    """Map tool name strings from the roster to actual tool objects."""
    resolved = []
    for name in tool_names:
        normalized = name.lower().replace(" ", "_").replace("-", "_")
        if normalized in TOOL_REGISTRY:
            resolved.append(TOOL_REGISTRY[normalized])
        else:
            logger.debug("Unrecognized tool %r — will mention in system prompt", name)
    return resolved or _COMMON_TOOLS


def build_agent(
    agent_name: str,
    role: str,
    skills: list[str],
    capabilities: list[str],
    tools: list[str],
    expertise: list[str],
) -> Optional[Any]:
    """Compile roster agent metadata into a live strands.Agent.

    Returns ``None`` if the strands SDK is not installed.
    """
    if not HAS_STRANDS:
        logger.warning("strands SDK not available; agent %s will use stub mode", agent_name)
        return None

    system_prompt = build_system_prompt(agent_name, role, skills, capabilities, tools, expertise)
    resolved = resolve_tools(tools)
    model = os.environ.get("AGENTIC_TEAM_TEST_MODEL", "us.anthropic.claude-sonnet-4-20250514")

    return StrandsAgent(
        model=model,
        system_prompt=system_prompt,
        tools=resolved,
        callback_handler=None,
    )


def call_agent(agent_instance: Any, message: str) -> str:
    """Invoke a strands.Agent and extract the text response."""
    if agent_instance is None:
        return f"[Stub response — strands SDK not available. Input: {message[:200]}]"
    try:
        result = agent_instance(message)
        if hasattr(result, "message"):
            return str(result.message).strip()
        return str(result).strip()
    except Exception as exc:
        logger.error("Agent call failed: %s", exc, exc_info=True)
        return f"[Agent error: {exc}]"


def generate_starter_prompts(
    agent_name: str, role: str, skills: list[str], expertise: list[str]
) -> list[str]:
    """Generate contextual starter prompts for an agent chat session.

    Uses template interpolation (no LLM call) to avoid latency on
    session creation.
    """
    prompts: list[str] = []

    if role:
        prompts.append(f"Describe how you approach your role as {role}.")

    if skills:
        skill = skills[0]
        prompts.append(f"Walk me through how you would use your {skill} skill.")

    if expertise:
        domain = expertise[0]
        prompts.append(f"What are the key challenges in {domain}?")

    if not prompts:
        prompts = [
            f"Introduce yourself and explain what you do, {agent_name}.",
            "What kind of tasks are you best suited for?",
            "Give me an example of how you'd handle a typical request.",
        ]

    return prompts[:3]
