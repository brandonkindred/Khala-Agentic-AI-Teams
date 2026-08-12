"""Shared utilities for branding team Strands SDK graphs.

Provides:
- Agent factory helpers (wired to the centralized LLM service)
- Conditional-edge callables for phase gating
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, Callable, Literal, Optional

from strands import Agent
from strands.multiagent.graph import GraphBuilder

from branding_team.models import BrandPhase
from llm_service import get_strands_model

if TYPE_CHECKING:
    from llm_service import LLMClientModel

# ---------------------------------------------------------------------------
# Agent-key tiers (per-phase LLM routing)
# ---------------------------------------------------------------------------

# Every pipeline agent passes an explicit ``agent_key`` instead of resolving
# ``build_agent``'s implicit "branding" default. The scheme is:
#
# - ``branding.<phase value>`` (via ``phase_agent_key``) for each phase's
#   specialist agents — reusing ``BrandPhase``'s own enum values so the tier
#   and the phase it routes can never drift apart. This groups each phase's
#   mix of open-ended strategic/creative work (e.g. Phase 1's
#   positioning_synthesizer, Phase 2's Storyteller) alongside its more
#   bounded extraction/list-generation specialists (e.g. Phase 5's
#   asset_wiki_planner) under one dial, so ops can tune per-phase cost/
#   quality via ``LLM_MODEL_branding.<phase>`` without a code change.
# - ``branding.compositor`` (``COMPOSITOR_AGENT_KEY``, via ``build_compositor``)
#   for the three phase-terminal join agents — ``visual_compositor``,
#   ``channel_compositor``, ``governance_compositor`` — that assemble a
#   phase's full set of upstream fragments into that phase's structured
#   output. This is a distinct role from any single phase's specialists
#   (broad-context synthesis across many fragments, not one bounded task)
#   and cuts across phases 3-5, so it gets its own tier rather than
#   inheriting its phase's key.
#
# ``BrandComplianceAgent`` (outside the graph) is deliberately excluded: it
# is a keyword-matching ``@dataclass`` with no LLM call, so no agent_key
# applies to it.
COMPOSITOR_AGENT_KEY = "branding.compositor"


def phase_agent_key(phase: BrandPhase) -> str:
    """Return the ``agent_key`` tier for *phase*'s specialist agents.

    Preconditions:
        ``phase`` is a ``BrandPhase`` member.
    Postconditions:
        Returns ``f"branding.{phase.value}"`` (e.g.
        ``"branding.strategic_core"`` for ``BrandPhase.STRATEGIC_CORE``).
    """
    return f"branding.{phase.value}"


# ---------------------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------------------

OutputMode = Literal["json", "text"]


# Every branding agent resolves a model keyed by (agent_key, output_mode).
# Building the graph instantiates ~40 agents per run; without memoisation
# each one constructs a fresh LLMClientModel. The model is a stateless
# wrapper over the cached LLM client, so one instance per (agent_key,
# output_mode) pair is safe to share across all agents and all runs. The
# Agents themselves are NOT cached — they carry per-invocation conversation
# state and must stay distinct per graph build.
#
# ``maxsize`` bounds the cache: the key space is the agent_key tiers below
# (five phase tiers + "branding.compositor" + the "branding" default +
# "branding_assistant") crossed with the two output modes — comfortably
# under 16 today, with headroom for a future tier without a cache eviction
# cliff.
@lru_cache(maxsize=16)
def _branding_model(agent_key: str, output_mode: OutputMode) -> "LLMClientModel":
    return get_strands_model(agent_key, response_format=output_mode)


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

    The backing model is the project's centralized ``LLMClientModel`` resolved
    via ``get_strands_model(agent_key, response_format=output_mode)`` — this
    routes through Ollama (or any configured ``LLM_PROVIDER``) and inherits
    retries, telemetry, and per-agent model routing (``LLM_MODEL_<agent_key>``).
    Passing a bare model string here would make Strands treat it as a Bedrock
    model ID and fail with ``NoCredentialsError`` outside AWS.

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
        ``branding.<phase>`` / ``branding.compositor`` tiers documented on
        :func:`phase_agent_key` and ``COMPOSITOR_AGENT_KEY`` below (see also
        the "LLM routing (agent_key tiers)" section of ``README.md``).
    """
    if output_mode not in ("json", "text"):
        raise ValueError(f"output_mode must be 'json' or 'text', got {output_mode!r}")
    kwargs: dict[str, Any] = {
        "name": name,
        "system_prompt": system_prompt,
        "model": _branding_model(agent_key, output_mode),
        "callback_handler": None,
    }
    if structured_output is not None:
        kwargs["structured_output_model"] = structured_output
    if tools:
        kwargs["tools"] = tools
    if description:
        kwargs["description"] = description
    return Agent(**kwargs)


def build_compositor(*, name: str, system_prompt: str, description: str = "") -> Agent:
    """Create a phase-terminal join agent on the shared ``branding.compositor`` tier.

    Thin wrapper over :func:`build_agent` that pins ``agent_key=COMPOSITOR_AGENT_KEY``
    so every phase's compositor (``visual_compositor``, ``channel_compositor``,
    ``governance_compositor``) shares one call site for that routing decision,
    instead of each phase file inlining ``build_agent(..., agent_key=COMPOSITOR_AGENT_KEY)``
    and risking the key drifting out of sync across phases. Always JSON mode
    (every compositor assembles its phase's fragments into a structured
    ``*Output`` document) and never ``structured_output=`` (a compositor's
    output shape is the full phase ``*Output`` model, assembled from
    prose-described fragments in the prompt, not a single upstream schema).

    Parameters
    ----------
    name:
        Unique agent name (used as graph node ID), e.g. ``"visual_compositor"``.
    system_prompt:
        Full system prompt describing what to assemble.
    description:
        Short human-readable description of the agent's purpose.

    Postconditions:
        Returns a ``build_agent(agent_key=COMPOSITOR_AGENT_KEY)`` result — see
        that function's contract.
    """
    return build_agent(
        name=name,
        system_prompt=system_prompt,
        description=description,
        agent_key=COMPOSITOR_AGENT_KEY,
    )


# ---------------------------------------------------------------------------
# Fan-out/fan-in wiring
# ---------------------------------------------------------------------------


def build_fan_out_fan_in(
    builder: GraphBuilder,
    agents: list[tuple[str, Callable[[], Agent]]],
    compositor: Any,
) -> None:
    """Wire a fan-out/fan-in topology onto *builder*.

    For each ``(node_id, factory)`` pair in *agents*: builds the node via
    ``factory()``, adds it to *builder*, wires an edge from it to
    *compositor*, and marks it as a graph entry point.

    Preconditions:
        *agents* is non-empty. *compositor* is a node handle already
        returned by ``builder.add_node(...)`` on the same *builder*.
    Postconditions:
        Every ``(node_id, factory)`` pair is added as a node on *builder*,
        edged to *compositor*, and registered as an entry point.
    """
    assert agents, "agents must be non-empty"
    for node_id, factory in agents:
        node = builder.add_node(factory(), node_id=node_id)
        builder.add_edge(node, compositor)
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
