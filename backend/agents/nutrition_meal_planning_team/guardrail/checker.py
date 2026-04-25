"""SPEC-007 §4.4 deterministic guardrail checker.

Implements steps 1, 2, 3, 5 of the check pipeline:

1. Parse each ingredient via ``ingredient_kb.parse_ingredient``.
2. Allergen check — intersect canonical food's ``allergen_tags`` with
   the profile's active allergen tags.
3. Dietary check — intersect ``dietary_tags`` with the profile's
   ``dietary_tags_forbid`` set.
5. Unknown-ingredient policy — fail closed:
   ``canonical_id is None`` and ``confidence < 0.85`` → hard reject;
   ``canonical_id is None`` and ``confidence >= 0.85`` → flag.

Steps 4 (medication interactions) and 6 (condition-specific flags)
land in W3/W4 and a later spec revision respectively.

Pure function. No I/O, no LLM, no clock. Same ``(profile, rec)`` →
byte-equal ``GuardrailResult``.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..ingredient_kb.catalog import get_catalog
from ..ingredient_kb.parser import parse_ingredient
from ..ingredient_kb.taxonomy import AllergenTag, DietaryTag
from ..ingredient_kb.types import CanonicalFood, ParsedIngredient
from ..models import ClientProfile, MealRecommendation
from .violations import GuardrailResult, Severity, Violation, ViolationReason

UNRESOLVED_CONFIDENCE_THRESHOLD = 0.85  # SPEC-007 §4.4 step 5


def check_recommendation(
    profile: ClientProfile,
    rec: MealRecommendation,
) -> GuardrailResult:
    resolution = profile.restriction_resolution
    active_allergens = frozenset(resolution.active_allergen_tags())
    active_dietary = frozenset(resolution.active_dietary_forbid())
    catalog = get_catalog()

    parsed = tuple(parse_ingredient(raw) for raw in rec.ingredients)

    hard: list[Violation] = []
    flags: list[Violation] = []

    for p in parsed:
        canonical: Optional[CanonicalFood] = catalog.get(p.canonical_id) if p.canonical_id else None
        is_low_confidence = p.confidence < UNRESOLVED_CONFIDENCE_THRESHOLD

        # Spec §4.4 step 5 plus the ParsedIngredient contract: any
        # confidence < 0.85 is unresolved/ambiguous, even when the
        # parser returned a canonical_id from a fuzzy match.
        if canonical is None or is_low_confidence:
            severity = (
                Severity.flag
                if canonical is None and not is_low_confidence
                else Severity.hard_reject
            )
            target = hard if severity is Severity.hard_reject else flags
            target.append(_unresolved(p, severity))
            continue

        for tag in _sorted_tags(canonical.allergen_tags & active_allergens):
            hard.append(_allergen(p, tag))

        for tag in _sorted_tags(canonical.dietary_tags & active_dietary):
            hard.append(_dietary(p, tag))

    return GuardrailResult(
        passed=len(hard) == 0,
        violations=tuple(hard),
        flags=tuple(flags),
        parsed_ingredients=parsed,
    )


def _sorted_tags(tags: Iterable) -> list:
    """Stable enum ordering — guarantees byte-equal results across runs."""
    return sorted(tags, key=lambda t: t.value)


def _unresolved(p: ParsedIngredient, severity: Severity) -> Violation:
    detail = (
        f"Could not confidently resolve '{p.raw}' (confidence={p.confidence:.2f})"
        if severity is Severity.hard_reject
        else f"Resolved structurally but no canonical match for '{p.raw}'"
    )
    return Violation(
        reason=ViolationReason.unresolved_ingredient,
        ingredient_raw=p.raw,
        canonical_id=None,
        tag=None,
        detail=detail,
        severity=severity,
    )


def _allergen(p: ParsedIngredient, tag: AllergenTag) -> Violation:
    return Violation(
        reason=ViolationReason.allergen,
        ingredient_raw=p.raw,
        canonical_id=p.canonical_id,
        tag=tag.value,
        detail=f"{p.raw} contains active allergen '{tag.value}'",
        severity=Severity.hard_reject,
    )


def _dietary(p: ParsedIngredient, tag: DietaryTag) -> Violation:
    return Violation(
        reason=ViolationReason.dietary_forbid,
        ingredient_raw=p.raw,
        canonical_id=p.canonical_id,
        tag=tag.value,
        detail=f"{p.raw} is forbidden by dietary rule '{tag.value}'",
        severity=Severity.hard_reject,
    )
