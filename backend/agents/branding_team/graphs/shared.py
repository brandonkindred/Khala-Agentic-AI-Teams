"""Shared utilities for branding team Strands SDK graphs.

Provides:
- Agent factory helpers (wired to the centralized LLM service)
- Conditional-edge callables for phase gating
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel
from strands import Agent

from branding_team.models import (
    BrandPhase,
    ChannelActivationOutput,
    GovernanceOutput,
    NarrativeMessagingOutput,
    StrategicCoreOutput,
    VisualIdentityOutput,
)
from shared.graph import OutputMode
from shared.graph import build_agent as _shared_build_agent

# ---------------------------------------------------------------------------
# Agent-key tiers (per-phase LLM routing)
# ---------------------------------------------------------------------------

# Every pipeline agent passes an explicit ``agent_key`` instead of resolving
# ``build_agent``'s implicit "branding" default: ``branding_<phase value>``
# (via ``phase_agent_key``) for each phase's specialist agents — reusing
# ``BrandPhase``'s own enum values so the tier and the phase it routes can
# never drift apart. Underscores keep the key a valid shell/Compose
# identifier so ``LLM_MODEL_<agent_key>`` can be exported
# (``LLM_MODEL_branding_strategic_core``). This groups each phase's mix of
# open-ended strategic/creative work (e.g. Phase 1's positioning_synthesizer,
# Phase 2's Storyteller) alongside its more bounded extraction/list-
# generation specialists (e.g. Phase 5's asset_wiki_planner) under one dial,
# so ops can tune per-phase cost/quality via ``LLM_MODEL_branding_<phase>``
# without a code change. No phase has a compositor node anymore (Phase 3's
# ``visual_compositor`` was the last one; its fragments are now merged
# deterministically in Python, same as Phases 4 and 5), so there is no
# separate compositor tier.
#
# ``BrandComplianceAgent`` (outside the graph) is deliberately excluded: it
# is a keyword-matching ``@dataclass`` with no LLM call, so no agent_key
# applies to it.


# ``str.isidentifier()`` accepts Unicode letters (PEP 3131), which are valid
# Python identifiers but not valid POSIX/Docker Compose env var names — so
# it under-enforces the shell/Compose guarantee ``phase_agent_key`` makes
# below. This is the ASCII-only shape env var names actually require.
_SHELL_SAFE_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def phase_agent_key(phase: BrandPhase) -> str:
    """Return the ``agent_key`` tier for *phase*'s specialist agents.

    Preconditions:
        ``phase`` is a ``BrandPhase`` member.
    Postconditions:
        Returns ``f"branding_{phase.value}"`` (e.g.
        ``"branding_strategic_core"`` for ``BrandPhase.STRATEGIC_CORE``).
        The result is a valid shell/Compose identifier (ASCII letters,
        digits, and underscores, not starting with a digit) so
        ``LLM_MODEL_<agent_key>`` can be set in env files and Compose.
        Raises ``ValueError`` rather than returning a key that would
        violate that guarantee — the mechanical ``f"branding_{phase.value}"``
        derivation has no other enforcement point, so a future ``BrandPhase``
        value containing e.g. a hyphen, space, or non-ASCII character is
        caught here instead of silently producing an unexportable env var
        name.
    """
    key = f"branding_{phase.value}"
    if not _SHELL_SAFE_KEY_RE.fullmatch(key):
        raise ValueError(f"phase_agent_key derived a non-shell-safe key {key!r} from {phase!r}")
    return key


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------


def build_agent(
    *,
    name: str,
    system_prompt: str,
    output_mode: OutputMode = "json",
    structured_output: Any | None = None,
    tools: list | None = None,
    description: str = "",
    agent_key: str = "branding",
) -> Agent:
    """Create a ``strands.Agent`` pre-configured for branding work.

    Thin wrapper over the cross-team :func:`shared.graph.build_agent` that
    pins the branding-specific default ``agent_key="branding"`` (the shared
    factory defaults to ``None``, which falls back to the bare ``LLM_MODEL``
    env var instead of branding's own tier). The backing model is the
    project's centralized ``LLMClientModel`` resolved via
    ``get_strands_model(agent_key, response_format=output_mode)`` — this
    routes through Ollama (or any configured ``LLM_PROVIDER``) and inherits
    retries, telemetry, and per-agent model routing (``LLM_MODEL_<agent_key>``).

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
        or template-based outputs (e.g. ``parse_planning_template``) where
        the consumer extracts structured data from prose itself.
    structured_output:
        Optional Pydantic ``BaseModel`` subclass for typed output. When set,
        ``output_mode`` is ignored — Strands routes through its
        ``structured_output_model`` flow which uses ``complete_json``
        regardless of mode.
    tools:
        Optional list of tools the agent may invoke.
    description:
        Short human-readable description of the agent's purpose.
    agent_key:
        LLM routing key passed to ``get_strands_model``, controlling which
        ``LLM_MODEL_<agent_key>`` override (if any) resolves the backing
        model. Defaults to ``"branding"`` for callers that don't need
        per-tier routing; every pipeline call site instead passes one of the
        ``branding_<phase>`` tiers documented on :func:`phase_agent_key`
        (see also the "LLM routing (agent_key tiers)" section of
        ``README.md``).
    """
    return _shared_build_agent(
        name=name,
        system_prompt=system_prompt,
        output_mode=output_mode,
        structured_output=structured_output,
        tools=tools,
        description=description,
        agent_key=agent_key,
    )


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

# Single source of truth for a phase's output model class, shared by
# ``orchestrator._PHASE_SPEC`` (graph-result extraction) and
# ``PhaseOutputCache`` (deserializing a cached phase output).
PHASE_OUTPUT_MODELS: Dict[BrandPhase, Type[BaseModel]] = {
    BrandPhase.STRATEGIC_CORE: StrategicCoreOutput,
    BrandPhase.NARRATIVE_MESSAGING: NarrativeMessagingOutput,
    BrandPhase.VISUAL_IDENTITY: VisualIdentityOutput,
    BrandPhase.CHANNEL_ACTIVATION: ChannelActivationOutput,
    BrandPhase.GOVERNANCE: GovernanceOutput,
}


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


# Human-readable display titles for each pipeline phase, keyed by BrandPhase.
# These mirror the hand-written phase list in the ``models``/``prompts`` module
# docstrings — kept as an explicit mapping (not derived mechanically from
# ``phase.value``) because several titles use "&" and phrasing that a simple
# ``phase.value.replace("_", " ").title()`` cannot reproduce (e.g. "Narrative &
# Messaging", not "Narrative Messaging"; "Visual & Expressive Identity", not
# "Visual Identity").
PHASE_TITLES: dict[BrandPhase, str] = {
    BrandPhase.STRATEGIC_CORE: "Strategic Core",
    BrandPhase.NARRATIVE_MESSAGING: "Narrative & Messaging",
    BrandPhase.VISUAL_IDENTITY: "Visual & Expressive Identity",
    BrandPhase.CHANNEL_ACTIVATION: "Experience & Channel Activation",
    BrandPhase.GOVERNANCE: "Governance & Evolution",
}


def phase_order_text() -> str:
    """Render the pipeline's phase order as "Phase N — Title" lines.

    Derives the list from ``PHASE_ORDER`` (execution order) and
    ``PHASE_TITLES`` (display names) instead of literal prose, so the two
    stay in sync automatically as the pipeline evolves.

    Preconditions:
        Every phase in ``PHASE_ORDER`` has an entry in ``PHASE_TITLES``.
    Postconditions:
        Returns a string with exactly ``len(PHASE_ORDER)`` lines, one per
        ``PHASE_ORDER`` entry in order, 1-indexed, formatted as
        ``"Phase {n} — {title}"`` and joined by ``"\n"`` (no trailing
        newline).
    """
    assert all(phase in PHASE_TITLES for phase in PHASE_ORDER), (
        "PHASE_TITLES must have a display title for every PHASE_ORDER entry"
    )
    return "\n".join(
        f"Phase {i} — {PHASE_TITLES[phase]}" for i, phase in enumerate(PHASE_ORDER, start=1)
    )


# ---------------------------------------------------------------------------
# Mission serialisation helper
# ---------------------------------------------------------------------------


def serialize_mission(mission: Any, *, include: Optional[frozenset[str]] = None) -> str:
    """Serialise a ``BrandingMission`` into a prompt-friendly string.

    Preconditions:
        - ``include``, when not ``None``, names fields present on ``mission``.
    Postconditions:
        - Returns JSON containing every field of ``mission`` when ``include``
          is ``None`` (default, unchanged behavior); returns JSON containing
          only the named fields when ``include`` is a (possibly empty)
          frozenset — the empty frozenset serialises to ``"{}"``.
    """
    return mission.model_dump_json(indent=2, include=include)
