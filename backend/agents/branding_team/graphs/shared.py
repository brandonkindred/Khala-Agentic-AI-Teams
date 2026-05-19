"""Shared utilities for branding team Strands SDK graphs.

Provides:
- Agent factory helpers (wired to the centralized LLM service)
- Conditional-edge callables for phase gating
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from strands import Agent

from branding_team.models import BrandPhase
from llm_service import get_strands_model

# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

# Map declared output mode → the LLM-service response_format the adapter
# expects. ``template`` is treated as text on the wire (the template parser
# does its own structured extraction from prose); the distinction is kept
# for readability at the call site and to make future mode-specific
# behaviors (e.g. stricter validation hooks) easy to add without churning
# every factory call.
_OUTPUT_MODE_TO_RESPONSE_FORMAT: dict[str, str] = {
    "json": "json",
    "text": "text",
    "template": "text",
}

OutputMode = Literal["json", "text", "template"]


def build_agent(
    *,
    name: str,
    system_prompt: str,
    output_mode: OutputMode = "json",
    structured_output: Any | None = None,
    tools: list | None = None,
    description: str = "",
) -> Agent:
    """Create a ``strands.Agent`` pre-configured for branding work.

    The backing model is the project's centralized ``LLMClientModel`` resolved
    via ``get_strands_model("branding", response_format=...)`` — this routes
    through Ollama (or any configured ``LLM_PROVIDER``) and inherits retries,
    telemetry, and per-agent model routing (``LLM_MODEL_branding``). Passing a
    bare model string here would make Strands treat it as a Bedrock model ID
    and fail with ``NoCredentialsError`` outside AWS.

    Parameters
    ----------
    name:
        Unique agent name (used as graph node ID).
    system_prompt:
        Full system prompt defining the agent's role and instructions.
    output_mode:
        Declarative shape of this agent's output, kept co-located with the
        system prompt that produces it. ``"json"`` (default) forces
        ``response_format=json_object`` on the wire — use when the downstream
        consumer ``json.loads`` / ``model_validate_json`` the assistant
        content. ``"text"`` uses prose mode — use for conversational replies
        with no downstream structured parsing. ``"template"`` is text on the
        wire but signals that the consumer extracts structured data from a
        template (e.g. ``parse_planning_template``). Picking the right mode
        is a per-agent contract; ``output_mode`` makes the contract visible
        at the factory call instead of buried in an audit of every
        ``response_format=`` keyword in the codebase.
    structured_output:
        Optional Pydantic ``BaseModel`` subclass for typed output. When set,
        ``output_mode`` is ignored — Strands routes through its
        ``structured_output_model`` flow which uses ``complete_json``
        regardless of mode.
    tools:
        Optional list of tools the agent may invoke.
    description:
        Short human-readable description of the agent's purpose.
    """
    if output_mode not in _OUTPUT_MODE_TO_RESPONSE_FORMAT:
        raise ValueError(
            f"output_mode must be one of {sorted(_OUTPUT_MODE_TO_RESPONSE_FORMAT)}, "
            f"got {output_mode!r}"
        )
    response_format = _OUTPUT_MODE_TO_RESPONSE_FORMAT[output_mode]
    kwargs: dict[str, Any] = {
        "name": name,
        "system_prompt": system_prompt,
        "model": get_strands_model("branding", response_format=response_format),
        "callback_handler": None,
    }
    if structured_output is not None:
        kwargs["structured_output_model"] = structured_output
    if tools:
        kwargs["tools"] = tools
    if description:
        kwargs["description"] = description
    return Agent(**kwargs)


# ---------------------------------------------------------------------------
# Phase-order helpers
# ---------------------------------------------------------------------------

PHASE_ORDER = [
    BrandPhase.STRATEGIC_CORE,
    BrandPhase.NARRATIVE_MESSAGING,
    BrandPhase.VISUAL_IDENTITY,
    BrandPhase.CHANNEL_ACTIVATION,
    BrandPhase.GOVERNANCE,
]


def phase_index(phase: BrandPhase) -> int:
    """Return 0-based position of *phase* in the pipeline."""
    try:
        return PHASE_ORDER.index(phase)
    except ValueError:
        return len(PHASE_ORDER)


def should_advance_past(phase_idx: int, target_phase: Optional[BrandPhase]) -> bool:
    """Return ``True`` if the pipeline should execute phases beyond *phase_idx*.

    When *target_phase* is ``None`` (run all), always returns True.
    """
    if target_phase is None:
        return True
    return phase_index(target_phase) > phase_idx


# ---------------------------------------------------------------------------
# Mission serialisation helper
# ---------------------------------------------------------------------------


def serialize_mission(mission: Any) -> str:
    """Serialise a ``BrandingMission`` into a prompt-friendly string."""
    return mission.model_dump_json(indent=2)
