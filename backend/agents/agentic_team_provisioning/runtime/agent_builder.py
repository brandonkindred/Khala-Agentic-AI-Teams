"""Compile an AgenticTeamAgent roster definition into a live strands.Agent.

Used by the interactive testing mode to turn declarative agent
definitions (role, skills, capabilities, tools, expertise) into
runnable agents that can respond to user messages.

The strands SDK is a hard dependency. The system will fail fast if it is not installed.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from strands import Agent as StrandsAgent
from strands_tools import current_time, http_request, python_repl

logger = logging.getLogger(__name__)

_COMMON_TOOLS: list[Any] = [http_request, python_repl, current_time]

# Registry mapping tool name strings from the roster to actual tool objects.
TOOL_REGISTRY: dict[str, Any] = {
    "http_request": http_request,
    "http": http_request,
    "python_repl": python_repl,
    "python": python_repl,
    "current_time": current_time,
}


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
    *,
    system_prompt_override: str | None = None,
) -> StrandsAgent:
    """Compile roster agent metadata into a live strands.Agent.

    Preconditions:
        * ``agent_name`` is non-empty.
    Postconditions:
        * Returns a ``strands.Agent`` whose system prompt is
          ``system_prompt_override`` when provided (the cognition-aware path
          folds advisory rules in here), else the prompt from
          :func:`build_system_prompt`.
    """
    system_prompt = (
        system_prompt_override
        if system_prompt_override is not None
        else build_system_prompt(agent_name, role, skills, capabilities, tools, expertise)
    )
    resolved = resolve_tools(tools)
    model = os.environ.get("AGENTIC_TEAM_TEST_MODEL", "us.anthropic.claude-sonnet-4-20250514")

    return StrandsAgent(
        model=model,
        system_prompt=system_prompt,
        tools=resolved,
        callback_handler=None,
    )


def call_agent(agent_instance: StrandsAgent, message: str) -> str:
    """Invoke a strands.Agent and extract the text response."""
    result = agent_instance(message)
    if hasattr(result, "message"):
        return str(result.message).strip()
    return str(result).strip()


def _read_cognition_context() -> dict[str, Any] | None:
    """Read the cognition side channel for the in-flight invoke, or ``None``.

    Postconditions: returns the proxy-injected ``{"rules": [...],
    "memory_digest": str}`` dict when a channel is open, else ``None`` — including
    when the ``agent_cognition`` package is absent from the image (no-op
    degradation, mirroring ``shared_agent_invoke``).
    """
    try:
        from agent_cognition.tools.channel import get_cognition_context
    except Exception:
        return None
    return get_cognition_context()


def render_cognition_prompt(base_prompt: str, cognition: dict[str, Any] | None) -> str:
    """Fold advisory rules + the memory digest into a base system prompt.

    Preconditions:
        * ``cognition`` is the :func:`_read_cognition_context` dict or ``None``.
    Postconditions:
        * Returns ``base_prompt`` unchanged when ``cognition`` is ``None`` or
          carries no advisory rules and an empty digest.
        * Otherwise returns ``base_prompt`` followed by an "Operating guidance"
          section listing only rules whose ``mode == "advisory"`` (highest
          ``priority`` first), then the memory digest when non-empty. Enforced
          rules are never rendered here — they are gated by the proxy/shim.
    """
    if not cognition:
        return base_prompt

    rules = cognition.get("rules") or []
    advisory = [r for r in rules if isinstance(r, dict) and r.get("mode") == "advisory"]
    advisory.sort(key=lambda r: r.get("priority", 0), reverse=True)
    digest = (cognition.get("memory_digest") or "").strip()

    if not advisory and not digest:
        return base_prompt

    sections = [base_prompt, "\n\n## Operating guidance (Agent Cognition Core)"]
    if advisory:
        sections.append("\nAdvisory guardrails — follow these unless the user overrides them:")
        for rule in advisory:
            text = str(rule.get("text", "")).strip()
            if text:
                sections.append(f"- {text}")
    if digest:
        sections.append(f"\nRelevant memory:\n{digest}")
    return "\n".join(sections)


def _build_writeback(agent_id: str, text: str) -> dict[str, Any] | None:
    """Build the cognition writeback for one invoke, or ``None``.

    Postconditions: returns a ``CognitionWriteback.model_dump(mode="json")`` dict
    carrying a single ``outcome`` :class:`MemoryEvent` summarizing the response;
    ``None`` when the ``agent_cognition`` package is unavailable (no-op
    degradation).
    """
    try:
        from agent_cognition.models import CognitionWriteback, EventKind, MemoryEvent
    except Exception:
        return None

    event = MemoryEvent(
        id=str(uuid.uuid4()),
        agent_id=agent_id,
        kind=EventKind.OUTCOME,
        content=text[:2000],
        occurred_at=datetime.now(tz=timezone.utc),
        source_run_id=f"{agent_id}#local",
        source_seq=0,
    )
    return CognitionWriteback(events=[event]).model_dump(mode="json")


def call_agent_with_cognition(
    agent_name: str,
    role: str,
    skills: list[str],
    capabilities: list[str],
    tools: list[str],
    expertise: list[str],
    message: str,
    *,
    agent_id: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Build, invoke, and produce a writeback for one cognition-aware invoke.

    Preconditions:
        * ``agent_name`` and ``message`` are non-empty.
    Postconditions:
        * Reads the cognition side channel for THIS invoke, renders advisory
          rules + memory digest into the system prompt, invokes the agent, and
          returns ``(response_text, writeback)``. When a channel is open
          ``writeback`` is a ``CognitionWriteback`` dump with at least one
          episodic ``MemoryEvent``; with no channel open (or cognition
          unavailable) the prompt is unchanged, ``writeback`` is ``None``, and the
          call behaves like the legacy path.
    """
    cognition = _read_cognition_context()
    base_prompt = build_system_prompt(agent_name, role, skills, capabilities, tools, expertise)
    prompt = render_cognition_prompt(base_prompt, cognition)
    agent_instance = build_agent(
        agent_name,
        role,
        skills,
        capabilities,
        tools,
        expertise,
        system_prompt_override=prompt,
    )
    text = call_agent(agent_instance, message)
    # Only emit a writeback when operating under the cognition envelope (a channel
    # is open). Off the gated path there is no proxy to persist it against.
    writeback = _build_writeback(agent_id or agent_name, text) if cognition is not None else None
    return text, writeback


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
