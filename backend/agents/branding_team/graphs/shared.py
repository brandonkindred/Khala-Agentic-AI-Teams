"""Shared utilities for branding team Strands SDK graphs.

Provides:
- Agent factory helpers (wired to the centralized LLM service)
- Conditional-edge callables for phase gating
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from strands import Agent
from strands.multiagent.graph import GraphBuilder, GraphNode

from branding_team.models import BrandPhase
from shared.graph import build_agent

# ---------------------------------------------------------------------------
# Agent-key tiers (per-phase LLM routing)
# ---------------------------------------------------------------------------

# Every pipeline agent passes an explicit ``agent_key`` rather than relying on
# ``shared.graph.build_agent``'s ``agent_key=None`` default. The scheme is:
#
# - ``branding_<phase value>`` (via ``phase_agent_key``) for each phase's
#   specialist agents — reusing ``BrandPhase``'s own enum values so the tier
#   and the phase it routes can never drift apart. Underscores keep the key
#   a valid shell/Compose identifier so ``LLM_MODEL_<agent_key>`` can be
#   exported (``LLM_MODEL_branding_strategic_core``). This groups each
#   phase's mix of open-ended strategic/creative work (e.g. Phase 1's
#   positioning_synthesizer, Phase 2's Storyteller) alongside its more
#   bounded extraction/list-generation specialists (e.g. Phase 5's
#   asset_wiki_planner) under one dial, so ops can tune per-phase cost/
#   quality via ``LLM_MODEL_branding_<phase>`` without a code change.
# - ``branding_compositor`` (``COMPOSITOR_AGENT_KEY``, via ``build_compositor``)
#   for the remaining phase-terminal join agent — ``visual_compositor`` —
#   that assembles a phase's full set of upstream fragments into that
#   phase's structured output. This is a distinct role from any single
#   phase's specialists (broad-context synthesis across many fragments,
#   not one bounded task), so it gets its own tier rather than inheriting
#   its phase's key. Phases 4 and 5 have no compositor: their fragments
#   are merged deterministically in Python instead.
#
# ``BrandComplianceAgent`` (outside the graph) is deliberately excluded: it
# is a keyword-matching ``@dataclass`` with no LLM call, so no agent_key
# applies to it.
COMPOSITOR_AGENT_KEY = "branding_compositor"


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
#
# The generic factory lives in ``shared.graph.build_agent`` (used by every
# team). This module only adds branding-specific wiring on top of it — see
# ``build_compositor`` below, which pins the shared ``branding_compositor``
# ``agent_key`` tier for the one phase-terminal join agent that needs it.


def build_compositor(
    *,
    name: str,
    system_prompt: str,
    description: str = "",
    structured_output: Any | None = None,
) -> Agent:
    """Create a phase-terminal join agent on the shared ``branding_compositor`` tier.

    Thin wrapper over :func:`build_agent` that pins ``agent_key=COMPOSITOR_AGENT_KEY``,
    keeping that routing decision at one call site rather than inlining
    ``build_agent(..., agent_key=COMPOSITOR_AGENT_KEY)`` in the phase file
    (``visual_compositor``, the only remaining compositor — Phases 4 and 5
    now merge their fragments deterministically in Python instead). Always
    JSON mode (every compositor assembles its phase's fragments into a
    structured ``*Output`` document).

    Parameters
    ----------
    name:
        Unique agent name (used as graph node ID), e.g. ``"visual_compositor"``.
    system_prompt:
        Full system prompt describing what to assemble.
    description:
        Short human-readable description of the agent's purpose.
    structured_output:
        Optional Pydantic model forwarded to :func:`build_agent`. Each
        compositor's output shape is its phase's ``*Output`` model, assembled
        from prose-described fragments in the prompt — passing that model
        here forces Strands' structured-output tool instead of relying on a
        prose "output JSON" reminder in the prompt.

    Postconditions:
        Returns a ``build_agent(agent_key=COMPOSITOR_AGENT_KEY)`` result — see
        that function's contract.
    """
    return build_agent(
        name=name,
        system_prompt=system_prompt,
        description=description,
        response_format="json",
        structured_output=structured_output,
        agent_key=COMPOSITOR_AGENT_KEY,
    )


# ---------------------------------------------------------------------------
# Fan-out/fan-in wiring
# ---------------------------------------------------------------------------


def build_fan_out_fan_in(
    builder: GraphBuilder,
    agents: list[tuple[str, Callable[[], Agent]]],
    fan_in_node: GraphNode,
) -> None:
    """Wire a fan-out/fan-in topology onto *builder*.

    For each ``(node_id, factory)`` pair in *agents*: builds the node via
    ``factory()``, adds it to *builder*, wires an edge from it to
    *fan_in_node*, and marks it as a graph entry point.

    *fan_in_node* is any collector node already on *builder* — a regular
    phase specialist (e.g. Phase 1's ``positioning_synthesizer``, Phase 3's
    ``CreativeDirector``) or a :func:`build_compositor` result. It is not
    required to be a compositor: this helper predates that concept and wires
    plain fan-out/fan-in topology generically.

    Preconditions:
        *agents* is non-empty. *fan_in_node* is the ``GraphNode`` already
        returned by ``builder.add_node(...)`` on the same *builder*.
    Postconditions:
        Every ``(node_id, factory)`` pair is added as a node on *builder*,
        edged to *fan_in_node*, and registered as an entry point.
    """
    assert agents, "agents must be non-empty"
    for node_id, factory in agents:
        node = builder.add_node(factory(), node_id=node_id)
        builder.add_edge(node, fan_in_node)
        builder.set_entry_point(node_id)


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


def serialize_mission(mission: Any) -> str:
    """Serialise a ``BrandingMission`` into a prompt-friendly string."""
    return mission.model_dump_json(indent=2)
