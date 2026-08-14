"""Compile resolved persona fields into a live strands.Agent.

Used by the interactive testing mode and pipeline runner to turn
join-at-read persona fields (role, skills, capabilities, tools, expertise)
into runnable agents that can respond to user messages.

The strands SDK is a hard dependency. The system will fail fast if it is not installed.
"""

from __future__ import annotations

import asyncio
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


def resolve_tools(tool_names: list[str], *, allow_common_tools_fallback: bool = True) -> list[Any]:
    """Map tool name strings from the roster to actual tool objects.

    Postconditions: returns the resolved tool objects. When nothing resolves, the
    interactive-testing convenience fallback (``_COMMON_TOOLS``) is returned only
    if ``allow_common_tools_fallback`` is true. The cognition/generated-agent path
    passes ``False`` so an agent whose manifest declares an empty
    ``cognition.tools`` never silently receives network / code-execution tools the
    cognition gate neither resolves nor audits.
    """
    resolved = []
    for name in tool_names:
        normalized = name.lower().replace(" ", "_").replace("-", "_")
        if normalized in TOOL_REGISTRY:
            resolved.append(TOOL_REGISTRY[normalized])
        else:
            logger.debug("Unrecognized tool %r — will mention in system prompt", name)
    if resolved:
        return resolved
    return _COMMON_TOOLS if allow_common_tools_fallback else []


def build_agent(
    agent_name: str,
    role: str,
    skills: list[str],
    capabilities: list[str],
    tools: list[str],
    expertise: list[str],
    *,
    system_prompt_override: str | None = None,
    allow_common_tools_fallback: bool = True,
) -> StrandsAgent:
    """Compile roster agent metadata into a live strands.Agent.

    Preconditions:
        * ``agent_name`` is non-empty.
    Postconditions:
        * Returns a ``strands.Agent`` whose system prompt is
          ``system_prompt_override`` when provided (the cognition-aware path
          folds advisory rules in here), else the prompt from
          :func:`build_system_prompt`. ``allow_common_tools_fallback`` is forwarded
          to :func:`resolve_tools`.
    """
    system_prompt = (
        system_prompt_override
        if system_prompt_override is not None
        else build_system_prompt(agent_name, role, skills, capabilities, tools, expertise)
    )
    resolved = resolve_tools(tools, allow_common_tools_fallback=allow_common_tools_fallback)
    model = os.environ.get("AGENTIC_TEAM_TEST_MODEL", "us.anthropic.claude-sonnet-4-20250514")

    return StrandsAgent(
        model=model,
        system_prompt=system_prompt,
        tools=resolved,
        callback_handler=None,
    )


def call_agent(agent_instance: StrandsAgent, message: str) -> str:
    """Invoke a strands.Agent and extract the text response.

    ``str(AgentResult)`` is the SDK operation that concatenates the textual
    content blocks from the result's message. ``result.message`` is the *raw*
    structured mapping (``{"role": ..., "content": [...]}``), so stringifying it
    would yield a dict repr rather than the model's reply — always go through
    ``str(result)``.
    """
    result = agent_instance(message)
    return str(result).strip()


def _read_cognition_context() -> dict[str, Any] | None:
    """Read the cognition side channel for the in-flight invoke, or ``None``.

    Postconditions: returns the proxy-injected ``{"rules": [...],
    "memory_digest": str}`` dict when a channel is open, else ``None`` — including
    when the ``agent_cognition`` package is absent from the image (no-op
    degradation, mirroring ``shared.agent_invoke``).
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
        content=text,
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
        # No silent code-exec/network fallback on the cognition path: an agent only
        # gets tools it (and the cognition gate) declare, never _COMMON_TOOLS.
        allow_common_tools_fallback=False,
    )
    text = call_agent(agent_instance, message)
    # Only emit a writeback when operating under the cognition envelope (a channel
    # is open). Off the gated path there is no proxy to persist it against.
    writeback = _build_writeback(agent_id or agent_name, text) if cognition is not None else None
    return text, writeback


async def invoke_generated_agent(body: Any) -> dict[str, Any]:
    """Sandbox entrypoint for a generated agentic-team agent.

    A single shared callable serves every generated manifest, so the roster
    metadata travels in the request ``body`` (see
    ``models.GeneratedAgentInvokeInput``) — the dispatch shim calls this as
    ``invoke_generated_agent(body)`` and awaits the coroutine.

    **Async on purpose:** the invoke shim runs entrypoints inline on its event
    loop, and the underlying Strands model call is blocking. Running it directly
    would stall the loop and prevent the shim's ``asyncio.wait_for`` timeout from
    firing, so the blocking work is offloaded to a worker thread here (the same
    treatment the shim already gives the tool loop). ``asyncio.to_thread`` copies
    the current context, so the cognition side channel still reaches the worker.

    Known limitation: ``to_thread`` cancellation only stops the awaiting coroutine
    — the worker thread (and its in-flight model request) runs to completion, the
    same property the shim's own brokered tool loop carries. The blocking call is
    therefore bounded only by the model client's transport timeout, not by the
    shim's invoke deadline; a hung model can keep consuming quota / holding a
    worker slot after the client already saw a timeout. A deadline-propagating /
    cancellable model invocation is tracked as a follow-up.

    Binding caveat (tracked follow-up): the dispatch contract hands this function
    only the request body, never the resolved manifest/agent id, so it cannot look
    up the agent's immutable persisted roster definition. The persona *text* fields
    are therefore taken from the (caller-controlled) body — a generated manifest
    selects which agent is advertised, not an enforced persona. **Tools are not
    taken from the body**: the manifest declares ``cognition.tools = []`` and tool
    brokering isn't wired yet, so the runtime grants no tools (a caller can't
    escalate to ``python`` / ``http_request``). Binding the manifest to its stored
    definition lands with the cross-process invoke work, against the locked
    precedence contract in ``system_design/adr/ADR-015-invoke-generated-agent-persona-state-precedence.md``.

    Preconditions:
        * ``body`` may be any value. Non-dicts (including ``None``) coerce to
          ``{}`` before ``GeneratedAgentInvokeInput`` validation so a malformed
          payload never raises a ``TypeError`` at the dispatch boundary.
          ``agent_name`` and ``message`` are recommended and default when omitted.
    Postconditions:
        * Reconstructs the roster agent, runs it through the cognition-aware
          wrapper (advisory rules + memory digest from the open side channel steer
          the invoke) off the event loop, and returns ``{"output": <response
          text>}`` (or a marker-wrapped writeback envelope when a channel is open).
    """
    return await asyncio.to_thread(_invoke_generated_agent_sync, body)


def _invoke_generated_agent_sync(body: Any) -> dict[str, Any]:
    """Blocking core of :func:`invoke_generated_agent` (runs in a worker thread).

    Validates ``body`` against the declared invoke schema before touching the
    model: the sandbox dispatch does not enforce the manifest's Pydantic input
    schema, so a malformed body (e.g. ``skills`` as an int, a non-string
    ``message``) would otherwise raise deep inside prompt construction. A
    ``ValidationError`` here surfaces as a clean request error at the boundary.
    """
    from agent_team_studio.agentic_team_provisioning.models import GeneratedAgentInvokeInput

    spec = GeneratedAgentInvokeInput.model_validate(body if isinstance(body, dict) else {})
    text, writeback = call_agent_with_cognition(
        spec.agent_name,
        spec.role,
        spec.skills,
        spec.capabilities,
        # Runtime tools are NOT taken from the (caller-controlled) body: the
        # generated manifest declares ``cognition.tools = []`` and tool brokering
        # isn't wired for generated agents yet, so granting a body-supplied tool
        # (e.g. ``python``/``http_request``) would hand out an unaudited
        # code-exec/network capability that bypasses the brokered tool loop. Keep
        # it empty until roster-bound tool brokering lands (the deferred work).
        [],
        spec.expertise,
        spec.message,
        agent_id=spec.agent_id,
    )
    return _shape_invoke_result(text, writeback)


def _shape_invoke_result(text: str, writeback: dict[str, Any] | None) -> dict[str, Any]:
    """Shape the entrypoint return value, lifting any writeback into the envelope.

    Postconditions: returns ``{"output": text}`` when there is no writeback or the
    cognition package is unavailable; otherwise returns a marker-wrapped envelope
    (``agent_cognition.tools.envelope.wrap_writeback``) carrying the same output
    plus the writeback, so the invoke shim lifts the episodic events into the
    response's ``memory_events`` instead of dropping them.
    """
    output = {"output": text}
    if not writeback:
        return output
    try:
        from agent_cognition.tools.envelope import wrap_writeback
    except Exception:
        return output
    return wrap_writeback(output, writeback)


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
