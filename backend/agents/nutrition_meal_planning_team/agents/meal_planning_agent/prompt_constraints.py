"""SPEC-007 §4.6 — structured constraints block for the meal-planner prompt.

Pure function. No I/O, no LLM. Deterministic: identical inputs produce
byte-identical output (all tag lists sorted by enum value).
"""

from __future__ import annotations

from typing import List

from ...guardrail.interactions import get_interaction_policies
from ...ingredient_kb.taxonomy import InteractionTag
from ...models import ClinicalInfo, RestrictionResolution

_FOOTER = "If you are unsure whether an ingredient violates a restriction, do not include it. We will reject it anyway."


def render_constraints_block(
    restriction_resolution: RestrictionResolution,
    clinical: ClinicalInfo,
) -> str:
    """Render the structured constraints block for the meal-planner prompt.

    Preconditions:
        ``restriction_resolution`` is a valid SPEC-006 resolution.
        ``clinical`` is a valid ``ClinicalInfo``.

    Postconditions:
        Returns a deterministic multi-line constraints block, or ``""``
        when the profile has no active restrictions and no medication
        interaction policies — avoids prompt bloat for unconstrained
        profiles.

    Invariants:
        All tag lists are sorted by ``.value`` (string sort). Identical
        inputs always produce byte-identical output.
    """
    sections: list[str] = []

    allergen_tags = sorted(restriction_resolution.active_allergen_tags(), key=lambda t: t.value)
    dietary_tags = sorted(restriction_resolution.active_dietary_forbid(), key=lambda t: t.value)

    med_hard_tags, med_flag_lines = _medication_constraints(clinical.medications)

    allergen_lines = [t.value for t in allergen_tags]
    for tag in med_hard_tags:
        if tag.value not in allergen_lines:
            allergen_lines.append(tag.value)
    allergen_lines.sort()

    if allergen_lines:
        body = "\n".join(f"  - {tag}" for tag in allergen_lines)
        sections.append(
            f"FORBIDDEN allergens (must not appear in any ingredient, by any name):\n{body}"
        )

    if dietary_tags:
        body = "\n".join(f"  - {t.value}" for t in dietary_tags)
        sections.append(f"FORBIDDEN dietary categories (must not appear):\n{body}")

    exemptions = _exemptions(restriction_resolution)
    if exemptions:
        body = "\n".join(f"  - {line}" for line in exemptions)
        sections.append(f"EXEMPTIONS (allowed despite a FORBIDDEN category above):\n{body}")

    flag_lines = list(med_flag_lines)
    for r in restriction_resolution.resolved:
        if r.soft_constraint:
            entry = f"{r.soft_constraint} (soft preference)"
            if entry not in flag_lines:
                flag_lines.append(entry)
    flag_lines.sort()

    if flag_lines:
        body = "\n".join(f"  - {line}" for line in flag_lines)
        sections.append(f"FLAG-ONLY (avoid when possible, not a hard ban):\n{body}")

    shorthands = _dietary_shorthands(restriction_resolution)
    if shorthands:
        body = "\n".join(f"  - {s}" for s in shorthands)
        sections.append(f"DIETARY SHORTHANDS (the above rules come from):\n{body}")

    warnings = _warnings(restriction_resolution, clinical)
    if warnings:
        body = "\n".join(f"  - {w}" for w in warnings)
        sections.append(f"WARNINGS:\n{body}")

    if not sections:
        return ""

    inner = "\n\n".join(sections)
    return f"=== DIETARY CONSTRAINTS (MUST OBEY) ===\n\n{inner}\n\n{_FOOTER}\n\n=== END DIETARY CONSTRAINTS ==="


def _exemptions(resolution: RestrictionResolution) -> List[str]:
    lines: list[str] = []
    for r in resolution.resolved:
        if r.dietary_allergen_exemptions:
            exempt_tags = sorted(r.dietary_allergen_exemptions, key=lambda t: t.value)
            forbid_tags = sorted(r.dietary_tags_forbid, key=lambda t: t.value)
            exempt_str = ", ".join(t.value for t in exempt_tags)
            forbid_str = ", ".join(t.value for t in forbid_tags)
            lines.append(f'{exempt_str} ARE allowed despite "{forbid_str}" forbidden ({r.raw})')
    lines.sort()
    return lines


def _medication_constraints(
    medications: List[str],
) -> tuple[list[InteractionTag], list[str]]:
    """Return (hard_tags, flag_lines) from medication interaction policies."""
    if not medications:
        return [], []

    known, _unknown = get_interaction_policies(medications)
    if not known:
        return [], []

    hard_tags_set: set[InteractionTag] = set()
    flag_by_tag: dict[str, list[str]] = {}

    for med, policy in sorted(known.items()):
        hard_tags_set.update(policy.hard)
        for tag in policy.flag:
            note_suffix = f": {policy.note}" if policy.note else ""
            flag_by_tag.setdefault(tag.value, []).append(f"{med}{note_suffix}")

    hard_tags = sorted(hard_tags_set, key=lambda t: t.value)

    flag_lines: list[str] = []
    for tag_val in sorted(flag_by_tag.keys()):
        sources = flag_by_tag[tag_val]
        if len(sources) == 1:
            flag_lines.append(f"{tag_val} ({sources[0]})")
        else:
            joined = "; ".join(sources)
            flag_lines.append(f"{tag_val} ({joined})")

    return hard_tags, flag_lines


def _dietary_shorthands(resolution: RestrictionResolution) -> List[str]:
    seen: set[str] = set()
    lines: list[str] = []
    for r in resolution.resolved:
        if r.source == "shorthand" and r.raw not in seen:
            seen.add(r.raw)
            lines.append(r.raw)
    lines.sort()
    return lines


def _warnings(
    resolution: RestrictionResolution,
    clinical: ClinicalInfo,
) -> List[str]:
    lines: list[str] = []
    for amb in resolution.ambiguous:
        lines.append(f'"{amb.raw}" is ambiguous — applying strictest interpretation until resolved')
    for raw in sorted(resolution.unresolved):
        lines.append(f'"{raw}" could not be resolved — treating as unrecognized restriction')
    if clinical.medications_freetext:
        joined = ", ".join(sorted(clinical.medications_freetext))
        lines.append(f"Unrecognized medications (check with clinician): {joined}")
    return lines
