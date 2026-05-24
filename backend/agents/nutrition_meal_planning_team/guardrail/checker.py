"""SPEC-007 §4.4 deterministic guardrail checker.

Implements steps 1–5 of the check pipeline:

1. Parse each ingredient via ``ingredient_kb.parse_ingredient``.
2. Allergen check — intersect canonical food's ``allergen_tags`` with
   the profile's active allergen tags.
3. Dietary check — intersect ``dietary_tags`` with the profile's
   ``dietary_tags_forbid`` set.
4. Medication-interaction check — intersect ``interaction_tags`` with
   the active medication policies from ``interactions.yaml``.
5. Unknown-ingredient policy — fail closed:
   ``canonical_id is None`` and ``confidence < 0.85`` → hard reject;
   ``canonical_id is None`` and ``confidence >= 0.85`` → flag.

Step 6 (condition-specific flags) lands in a later spec revision.

Pure function. No I/O, no LLM, no clock. Same ``(profile, rec)`` →
byte-equal ``GuardrailResult``.
"""

from __future__ import annotations

from typing import Iterable, Optional

from ..ingredient_kb.catalog import get_catalog
from ..ingredient_kb.parser import parse_ingredient
from ..ingredient_kb.taxonomy import AllergenTag, DietaryTag, InteractionTag
from ..ingredient_kb.types import CanonicalFood, ParsedIngredient
from ..models import ClientProfile, MealRecommendation
from .interactions import InteractionPolicy, get_interaction_policies
from .violations import GuardrailResult, Severity, Violation, ViolationReason

UNRESOLVED_CONFIDENCE_THRESHOLD = 0.85  # SPEC-007 §4.4 step 5


def check_recommendation(
    profile: ClientProfile,
    rec: MealRecommendation,
) -> GuardrailResult:
    resolution = profile.restriction_resolution
    active_allergens = frozenset(resolution.active_allergen_tags())
    catalog = get_catalog()
    med_policies, unknown_meds = get_interaction_policies(profile.clinical.medications)

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

        # Per-food: a resolution's allergen exemption (e.g. pescatarian +
        # fish) drops its dietary forbid for this food only.
        applicable_dietary = resolution.applicable_dietary_forbid(
            frozenset(canonical.allergen_tags)
        )
        for tag in _sorted_tags(canonical.dietary_tags & applicable_dietary):
            hard.append(_dietary(p, tag))

        # Step 4: medication-interaction check
        for policy in med_policies.values():
            for tag in _sorted_tags(canonical.interaction_tags & policy.hard):
                hard.append(_interaction_hard(p, tag, policy))
            for tag in _sorted_tags(canonical.interaction_tags & policy.flag):
                flags.append(_interaction_flag(p, tag, policy))

    for med_name in sorted(unknown_meds):
        flags.append(_unknown_medication(med_name))

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


def _interaction_hard(
    p: ParsedIngredient, tag: InteractionTag, policy: InteractionPolicy
) -> Violation:
    return Violation(
        reason=ViolationReason.interaction_hard,
        ingredient_raw=p.raw,
        canonical_id=p.canonical_id,
        tag=tag.value,
        detail=f"{p.raw} interacts with medication '{policy.medication}' (tag '{tag.value}')",
        severity=Severity.hard_reject,
    )


def _interaction_flag(
    p: ParsedIngredient, tag: InteractionTag, policy: InteractionPolicy
) -> Violation:
    return Violation(
        reason=ViolationReason.interaction_flag,
        ingredient_raw=p.raw,
        canonical_id=p.canonical_id,
        tag=tag.value,
        detail=f"{p.raw} may interact with medication '{policy.medication}' (tag '{tag.value}')",
        severity=Severity.flag,
    )


def _unknown_medication(med_name: str) -> Violation:
    return Violation(
        reason=ViolationReason.interaction_flag,
        ingredient_raw="",
        canonical_id=None,
        tag=None,
        detail=f"Medication '{med_name}' is not in the known interactions database; review with prescriber",
        severity=Severity.flag,
    )
