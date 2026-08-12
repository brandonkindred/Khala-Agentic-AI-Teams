"""Prompt-spec schema, template renderer, and per-agent prompt data for
``branding_team.agents``'s ``make_*`` factories.

Mirrors the data-driven prompt-construction idiom proven in
``branding_team.assistant.prompts`` (``_PHASE_ITEMS``/``_PHASE_INTROS``/
``_phase_section()``): a prompt is represented as an explicit data structure
and rendered by a single pure function, instead of being hand-written prose
duplicated across factories. As in that module, the prompt *data* lives here
next to the renderer rather than in the consumer (``agents.py``) that wires
rendered text into ``build_agent()`` calls — the same split as
``assistant/prompts.py`` (data + renderer) vs. ``assistant/agent.py``
(consumer).

The schema itself is intentionally *not* shared with
``assistant.prompts``'s ``BrandPhase``-keyed mechanism, despite the
surface-level similarity — the two solve different rendering problems.
``assistant/prompts.py`` assembles one ordered, 5-part guided-flow document
from phase-keyed fragments that reference neighboring phases by number
(``_PHASE_DEPENDS_ON_PREV``, ``_PHASE_GATE_CONDITIONS``, joined via
``PHASE_ORDER``). ``agents.py``'s ~38 factories each need one independent,
unordered, single-role prompt (role + intro + numbered output fields +
optional closing) with no neighbor/position concept at all. Forcing both
shapes through one schema would mean branching on two incompatible calling
conventions inside a single renderer — more complexity, not less. What
*does* carry over from ``assistant/prompts.py`` is the idiom: role, output
fields, and cardinality constraints as data; a small renderer that turns the
data into the same hand-written-prose shape the factories used before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class PromptField:
    """One output field an agent must produce.

    Preconditions:
        ``name`` and ``description`` are non-empty. ``cardinality``, when
        given, is a short quantity phrase (e.g. ``"3-5"``) describing how
        many items the field expects — used for list-typed fields whose
        prompt names a count, mirroring prose like "3-5 core values".
    """

    name: str
    description: str
    cardinality: Optional[str] = None


@dataclass(frozen=True)
class PromptSpec:
    """Data-driven description of a ``make_*`` factory's system prompt.

    Preconditions:
        ``role`` is a full indefinite-article noun phrase (e.g. ``"a Purpose
        & Vision Writer"``, ``"an Iconography Director"``) — the article is
        authored explicitly rather than inferred from the first letter,
        since English a/an usage has exceptions (e.g. "an SEO Specialist")
        that a first-letter heuristic gets wrong. ``intro`` is non-empty.
        ``fields`` is non-empty.
    """

    role: str
    intro: str
    fields: List[PromptField]
    closing: Optional[str] = None


def render_prompt(spec: PromptSpec) -> str:
    """Render *spec* into the hand-written-prose prompt shape it replaces.

    Preconditions:
        ``spec.fields`` is non-empty.
    Postconditions:
        Returns ``"You are {role}. {intro}"`` on the first line, followed by
        one 1-indexed numbered line per field
        (``"{n}. {name} — {description}"``, with ``" ({cardinality})"``
        appended when the field declares one), followed by ``spec.closing``
        as a final line when set. Lines are joined with ``"\\n"`` and no
        trailing newline is added. Fidelity with the pre-migration
        hand-written prompts is pinned by ``tests/test_agents_prompts.py``.
    """
    assert spec.fields, "PromptSpec.fields must be non-empty"
    lines = [f"You are {spec.role}. {spec.intro}"]
    for i, prompt_field in enumerate(spec.fields, start=1):
        cardinality_suffix = f" ({prompt_field.cardinality})" if prompt_field.cardinality else ""
        lines.append(f"{i}. {prompt_field.name} — {prompt_field.description}{cardinality_suffix}")
    if spec.closing:
        lines.append(spec.closing)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-agent prompt data — proof-of-concept for the Phase 1 Purpose & Vision
# Writer and Phase 3 Iconography Director factories in ``agents.py``. Further
# ``make_*`` factories are migrated to specs here in follow-on sub-issues.
# ---------------------------------------------------------------------------

PURPOSE_VISION_PROMPT_SPEC = PromptSpec(
    role="a Purpose & Vision Writer",
    intro="Given a branding mission, write three things:",
    fields=[
        PromptField("brand_purpose", "why the company exists (one sentence)"),
        PromptField("mission_statement", "what the company does for its audience (one sentence)"),
        PromptField("vision_statement", "the aspirational future state (one sentence)"),
    ],
    closing="Be concise, inspiring, and specific to the company.",
)

ICONOGRAPHY_DIRECTOR_PROMPT_SPEC = PromptSpec(
    role="an Iconography Director",
    intro="Based on the winning moodboard, define:",
    fields=[
        PromptField(
            "iconography_style", "describe the icon aesthetic (line weight, corner radius, fill)"
        ),
        PromptField(
            "illustration_style", "describe the illustration approach (flat, isometric, etc.)"
        ),
    ],
)
