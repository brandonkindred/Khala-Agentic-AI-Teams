"""SPEC-007 §4.5 / §4.6 — regeneration prompt for rejected meal suggestions.

Pure function. No I/O, no LLM. Deterministic: identical inputs produce
byte-identical output (violation entries sorted by ``(ingredient_raw, tag)``).
"""

from __future__ import annotations

import json
from typing import Sequence

from ...guardrail.violations import Violation
from ...models import ClientProfile, MealRecommendation
from .prompt_constraints import render_constraints_block

REGENERATION_SYSTEM_PROMPT = (
    "You are a meal planning expert. A previous meal suggestion was rejected "
    "by the safety guardrail because it contained forbidden ingredients.\n\n"
    "Generate exactly ONE replacement meal suggestion as a JSON object.\n"
    "You MUST NOT use any ingredient from the FORBIDDEN list.\n"
    "You MUST obey every dietary constraint listed.\n"
    "Output valid JSON matching the schema provided. "
    "No markdown fences. No prose outside the JSON."
)


def build_regeneration_prompt(
    profile: ClientProfile,
    original: MealRecommendation,
    violations: Sequence[Violation],
) -> str:
    """Build the user prompt for a single-meal regeneration request.

    Preconditions:
        ``violations`` is non-empty (caller checked the guardrail result).
        ``profile`` carries a valid ``restriction_resolution`` and ``clinical``.

    Postconditions:
        Returns a deterministic multi-part prompt string containing:
        - An explicit FORBIDDEN list derived from ``violations``
        - The full profile constraints block (via ``render_constraints_block``)
        - The original meal's slot context (name, meal_type, suggested_date)
        - A JSON-output instruction with the ``MealRecommendation`` schema

    Invariants:
        Identical inputs produce byte-identical output. Violation entries
        are deduplicated by ``(ingredient_raw, tag)`` and sorted.
    """
    parts: list[str] = []

    parts.append(_forbidden_section(violations))

    constraints = render_constraints_block(profile.restriction_resolution, profile.clinical)
    if constraints:
        parts.append(constraints)

    parts.append(_original_context(original))
    parts.append(_instruction(original))

    return "\n\n".join(parts)


def _forbidden_section(violations: Sequence[Violation]) -> str:
    seen: set[tuple[str, str | None]] = set()
    lines: list[str] = []
    for v in violations:
        key = (v.ingredient_raw, v.tag)
        if key in seen:
            continue
        seen.add(key)
        if v.tag:
            lines.append(f"  - {v.ingredient_raw} (tag: {v.tag})")
        else:
            lines.append(f"  - {v.ingredient_raw}")
    lines.sort()
    body = "\n".join(lines)
    return f"FORBIDDEN INGREDIENTS (do NOT use any of these, by any name or derivative):\n{body}"


def _original_context(original: MealRecommendation) -> str:
    date_str = original.suggested_date or "none"
    return (
        "Original meal (rejected — do NOT reuse its ingredients):\n"
        f"  Name: {original.name}\n"
        f"  Meal type: {original.meal_type}\n"
        f"  Suggested date: {date_str}"
    )


def _instruction(original: MealRecommendation) -> str:
    schema_json = json.dumps(MealRecommendation.model_json_schema(), separators=(",", ":"))
    meal_type = original.meal_type or "meal"
    return (
        f"Generate exactly ONE replacement {meal_type} that:\n"
        "1. Does NOT contain any FORBIDDEN ingredient listed above\n"
        "2. Obeys ALL dietary constraints above\n"
        "3. Is a reasonable, nutritious alternative to the rejected meal\n\n"
        f"Output valid JSON matching this schema:\n{schema_json}\n\n"
        "Output JSON only. No markdown fences. No prose outside the JSON."
    )
