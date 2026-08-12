"""Prompt-spec schema and template renderer for branding agent factories.

Mirrors the data-driven prompt-construction idiom proven in
``branding_team.assistant.prompts`` (``_PHASE_ITEMS``/``_PHASE_INTROS``/
``_phase_section()``): a prompt is represented as an explicit data structure
and rendered by a single pure function, instead of being hand-written prose
duplicated across factories. That mechanism is keyed by the shared, ordered
``BrandPhase`` enum, which doesn't fit here — each ``make_*`` factory in
``agents.py`` is an independent unit with no shared ordering key. What
carries over is the idiom itself: role, output fields, and cardinality
constraints as data; a renderer that turns the data into the same
hand-written-prose shape the factories used before.
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
