"""Shared utilities for branding team Strands SDK graphs.

Provides:
- LLM model configuration
- Agent factory helpers
- Conditional-edge callables for phase gating
"""

from __future__ import annotations

import os
from typing import Any, Optional

from strands import Agent

from branding_team.models import BrandPhase

# ---------------------------------------------------------------------------
# LLM model configuration
# ---------------------------------------------------------------------------

DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-20250514"


def get_model() -> str:
    """Return the LLM model string for branding agents.

    Reads ``BRANDING_LLM_MODEL`` env var, falling back to a sensible default.
    """
    return os.environ.get("BRANDING_LLM_MODEL", DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_agent(
    *,
    name: str,
    system_prompt: str,
    structured_output: Any | None = None,
    tools: list | None = None,
    description: str = "",
) -> Agent:
    """Create a ``strands.Agent`` pre-configured for branding work.

    Parameters
    ----------
    name:
        Unique agent name (used as graph node ID).
    system_prompt:
        Full system prompt defining the agent's role and instructions.
    structured_output:
        Optional Pydantic ``BaseModel`` subclass for typed output.
    tools:
        Optional list of tools the agent may invoke.
    description:
        Short human-readable description of the agent's purpose.
    """
    kwargs: dict[str, Any] = {
        "name": name,
        "system_prompt": system_prompt,
        "model": get_model(),
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
