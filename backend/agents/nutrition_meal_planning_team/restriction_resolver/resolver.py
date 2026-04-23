"""SPEC-006 restriction resolver.

Pure function ``resolve_restrictions(allergies, dietary_needs)`` that
turns user free-text lists into a :class:`RestrictionResolution` of
canonical tag sets.

Cascade (applied per input string, stop at first hit):

1. **Exact alias** — ``ingredient_kb.alias_index.lookup`` returns
   ``score == 1.0``. Produces a resolution with the catalog entry's
   allergen + dietary tags and the matched canonical id.
2. **Shorthand** — ``shorthand.yaml`` synonym hit (vegan, keto,
   gluten-free, …).
3. **Allergen category** — direct enum-name match (``tree nut``,
   ``shellfish``, ``gluten``, etc.).
4. **Ambiguity table** — curated entries with multiple plausible
   resolutions (``nuts``, ``seafood``, ``low-carb``). Emits an
   :class:`AmbiguousRestriction` the UI surfaces to the user. Default-
   strict: ``RestrictionResolution.active_*`` unions every candidate's
   tags so SPEC-007 can enforce pre-confirm.
5. **Fuzzy alias** — ``alias_index.lookup`` score ≥
   :data:`FUZZY_THRESHOLD` (0.85). The KB's own floor is 0.5 for
   ingredient-parser use; restrictions need the tighter gate.
6. **Unresolved** — falls into ``unresolved[]``.

Negation (``"no X"`` / ``"X-free"`` / ``"avoid X"``) is a
preprocessing step: the bare payload feeds the cascade; the raw
string is preserved on the resolved record.

No I/O, no LLM, no network. Safe to call from any layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ..ingredient_kb.catalog import get_alias_index, get_catalog
from ..ingredient_kb.normalizer import normalize
from ..ingredient_kb.taxonomy import AllergenTag
from ..ingredient_kb.version import KB_VERSION
from ..models import (
    AmbiguousRestriction,
    ResolvedRestriction,
    RestrictionResolution,
)
from . import metrics, negation, shorthand

FUZZY_THRESHOLD = 0.85


# Allergen-category direct names. Keys are already-normalized strings
# (depluralized, lowercased). Variants with/without hyphens cover both
# ``tree nut`` and ``tree-nut``.
_ALLERGEN_CATEGORY: Dict[str, AllergenTag] = {
    "peanut": AllergenTag.peanut,
    "tree nut": AllergenTag.tree_nut,
    "tree-nut": AllergenTag.tree_nut,
    "treenut": AllergenTag.tree_nut,
    "shellfish": AllergenTag.shellfish,
    "gluten": AllergenTag.gluten,
    "fish": AllergenTag.fish,
    "dairy": AllergenTag.dairy,
    "milk": AllergenTag.dairy,
    "egg": AllergenTag.egg,
    "soy": AllergenTag.soy,
    "soya": AllergenTag.soy,
    "wheat": AllergenTag.wheat,
    "sesame": AllergenTag.sesame,
    "mustard": AllergenTag.mustard,
    "celery": AllergenTag.celery,
    "sulfite": AllergenTag.sulfites,
    "sulphite": AllergenTag.sulfites,
    "lupin": AllergenTag.lupin,
    "mollusc": AllergenTag.mollusc,
    "mollusk": AllergenTag.mollusc,
}


@dataclass(frozen=True)
class _AmbiguityEntry:
    candidates: Tuple[ResolvedRestriction, ...]
    question: str


def _mk_candidate(
    raw: str,
    *,
    allergens: Tuple[AllergenTag, ...] = (),
    soft: Optional[str] = None,
    note: str = "",
    rule: str = "category",
) -> ResolvedRestriction:
    return ResolvedRestriction(
        raw=raw,
        allergen_tags=list(allergens),
        soft_constraint=soft,
        note=note,
        confidence=1.0,
        source="user",
        rule=rule,
    )


def _build_ambiguity_table() -> Dict[str, _AmbiguityEntry]:
    """Return ``{normalized_key: entry}``.

    Keys are the *normalized* form of the raw input so the cascade can
    look up in O(1) after normalizing once.
    """
    table: Dict[str, _AmbiguityEntry] = {}

    # "nuts" / "nut" — both normalize to "nut"
    nuts_key = normalize("nuts")
    table[nuts_key] = _AmbiguityEntry(
        candidates=(
            _mk_candidate("nuts", allergens=(AllergenTag.peanut,)),
            _mk_candidate("nuts", allergens=(AllergenTag.tree_nut,)),
            _mk_candidate("nuts", allergens=(AllergenTag.peanut, AllergenTag.tree_nut)),
        ),
        question=(
            "By 'nuts', do you mean peanuts, tree nuts, or both? "
            "Peanuts and tree nuts are separate allergens."
        ),
    )

    # "seafood"
    seafood_key = normalize("seafood")
    table[seafood_key] = _AmbiguityEntry(
        candidates=(
            _mk_candidate("seafood", allergens=(AllergenTag.fish,)),
            _mk_candidate("seafood", allergens=(AllergenTag.shellfish,)),
            _mk_candidate("seafood", allergens=(AllergenTag.mollusc,)),
            _mk_candidate(
                "seafood",
                allergens=(
                    AllergenTag.fish,
                    AllergenTag.shellfish,
                    AllergenTag.mollusc,
                ),
            ),
        ),
        question=("Does 'seafood' include all of fish, shellfish, and molluscs?"),
    )

    # "low-carb" / "low carb" — normalize drops nothing, so cover both
    for label in ("low-carb", "low carb"):
        table[normalize(label)] = _AmbiguityEntry(
            candidates=(
                _mk_candidate(
                    label,
                    soft="low_carb",
                    note="advisory reduction only",
                    rule="shorthand",
                ),
                _mk_candidate(
                    label,
                    soft="low_carb",
                    note="keto-style hard restriction",
                    rule="shorthand",
                ),
            ),
            question="How strict is your low-carb preference?",
        )

    return table


_AMBIGUITY_TABLE: Optional[Dict[str, _AmbiguityEntry]] = None


def _ambiguity_lookup(norm: str) -> Optional[_AmbiguityEntry]:
    global _AMBIGUITY_TABLE
    if _AMBIGUITY_TABLE is None:
        _AMBIGUITY_TABLE = _build_ambiguity_table()
    return _AMBIGUITY_TABLE.get(norm)


def _cascade(
    raw: str, norm: str
) -> Tuple[Optional[ResolvedRestriction], Optional[AmbiguousRestriction]]:
    """Run rules 1–5 for a single normalized key. Returns
    ``(resolved, ambiguous)`` — exactly one is non-None on success,
    both None on a miss.
    """
    alias_index = get_alias_index()
    catalog = get_catalog()
    match = alias_index.lookup(norm)
    if match is not None and match.score >= 1.0:
        food = catalog[match.canonical_id]
        return (
            ResolvedRestriction(
                raw=raw,
                allergen_tags=list(food.allergen_tags),
                dietary_tags_forbid=list(food.dietary_tags),
                matched_canonical_ids=[match.canonical_id],
                confidence=1.0,
                source="user",
                rule="exact_alias",
            ),
            None,
        )

    # Rule 2: shorthand
    sh = shorthand.lookup(norm)
    if sh is not None:
        metrics.record_shorthand(sh.name)
        return (
            ResolvedRestriction(
                raw=raw,
                allergen_tags=list(sh.forbid_allergen),
                dietary_tags_forbid=list(sh.forbid_dietary),
                confidence=1.0,
                source="shorthand",
                rule="shorthand",
                soft_constraint=sh.soft_constraint,
                note=sh.note or "",
            ),
            None,
        )

    # Rule 3: allergen category
    cat = _ALLERGEN_CATEGORY.get(norm)
    if cat is not None:
        return (
            ResolvedRestriction(
                raw=raw,
                allergen_tags=[cat],
                confidence=1.0,
                source="user",
                rule="category",
            ),
            None,
        )

    # Rule 4: ambiguity table
    amb = _ambiguity_lookup(norm)
    if amb is not None:
        return (
            None,
            AmbiguousRestriction(
                raw=raw,
                candidates=list(amb.candidates),
                question=amb.question,
            ),
        )

    # Rule 5: fuzzy alias match
    if match is not None and match.score >= FUZZY_THRESHOLD:
        food = catalog[match.canonical_id]
        return (
            ResolvedRestriction(
                raw=raw,
                allergen_tags=list(food.allergen_tags),
                dietary_tags_forbid=list(food.dietary_tags),
                matched_canonical_ids=[match.canonical_id],
                confidence=match.score,
                source="user",
                rule="fuzzy",
            ),
            None,
        )

    return None, None


def _resolve_one(
    raw: str,
) -> Tuple[Optional[ResolvedRestriction], Optional[AmbiguousRestriction], Optional[str]]:
    """Return ``(resolved, ambiguous, unresolved_raw)`` — exactly one
    of the first two is non-None, or ``unresolved_raw`` is set.
    """
    if not raw or not raw.strip():
        return None, None, None  # silently drop empty inputs

    # First pass: run cascade on the raw as-is. Shorthand entries like
    # ``"gluten-free"`` must win over negation-stripping to
    # ``"gluten"``, since the shorthand is the more specific resolution.
    norm = normalize(raw)
    if norm:
        resolved, ambiguous = _cascade(raw, norm)
        if resolved is not None:
            metrics.record_outcome("resolved", rule=resolved.rule)
            return resolved, None, None
        if ambiguous is not None:
            metrics.record_outcome("ambiguous", rule="ambiguity_table")
            return None, ambiguous, None

    # Second pass: strip negation markers ("no X", "avoid X", "X-free")
    # and retry the cascade on the bare payload. Raw is preserved.
    neg = negation.detect(raw)
    if neg.is_negation:
        norm2 = normalize(neg.stripped)
        if norm2:
            resolved, ambiguous = _cascade(raw, norm2)
            if resolved is not None:
                metrics.record_outcome("resolved", rule=resolved.rule)
                return resolved, None, None
            if ambiguous is not None:
                metrics.record_outcome("ambiguous", rule="ambiguity_table")
                return None, ambiguous, None

    # Rule 6: unresolved
    metrics.record_outcome("unresolved", rule="unresolved")
    return None, None, raw


def resolve_restrictions(
    allergies: List[str],
    dietary_needs: List[str],
) -> RestrictionResolution:
    """Resolve raw restriction strings to canonical tag sets.

    Both lists feed the same cascade — users mislabel routinely, so
    divergent rules per list would surprise. Duplicate raw strings
    across the lists are resolved once (first occurrence wins).
    """
    resolved: List[ResolvedRestriction] = []
    ambiguous: List[AmbiguousRestriction] = []
    unresolved: List[str] = []
    seen: set[str] = set()

    for raw in list(allergies or []) + list(dietary_needs or []):
        if raw is None:
            continue
        key = (raw or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        r, a, u = _resolve_one(raw)
        if r is not None:
            resolved.append(r)
        elif a is not None:
            ambiguous.append(a)
        elif u is not None:
            unresolved.append(u)

    return RestrictionResolution(
        resolved=resolved,
        ambiguous=ambiguous,
        unresolved=unresolved,
        kb_version=KB_VERSION,
        resolved_at=datetime.now(tz=timezone.utc).isoformat(),
    )
