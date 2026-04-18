"""Loader for the canonical foods catalog + alias index.

SPEC-005 §4.6. Loads ``canonical_foods.yaml`` and ``densities.yaml``
at module import via ``functools.lru_cache`` so every downstream
caller pays the load cost once.

Public shape: ``get_catalog() -> dict[str, CanonicalFood]`` and
``get_alias_index() -> AliasIndex``. Callers should go through
``parser.parse_ingredient`` in normal flow; direct catalog access is
for CLI tools and tests.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from .errors import KBError
from .normalizer import normalize
from .taxonomy import AllergenTag, DietaryTag, InteractionTag
from .types import AliasMatch, CanonicalFood, PurchaseUnit

_DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_yaml(name: str):
    path = _DATA_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"ingredient_kb data missing: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _parse_tag_set(raw, enum_cls):
    out = set()
    for tag in raw or []:
        try:
            out.add(enum_cls(tag))
        except ValueError as exc:
            raise KBError(f"canonical_foods.yaml: unknown {enum_cls.__name__} tag {tag!r}") from exc
    return frozenset(out)


def _parse_canonical_food(row: dict) -> CanonicalFood:
    food_id = row["id"]
    display_name = row.get("display_name") or food_id.replace("_", " ").title()
    aliases = tuple(row.get("aliases") or ())
    # Lint: display_name lowercased must be in aliases.
    if display_name.lower() not in {a.lower() for a in aliases}:
        aliases = aliases + (display_name.lower(),)

    pu_raw = row.get("purchase_unit") or None
    purchase_unit = None
    if pu_raw and pu_raw.get("unit"):
        purchase_unit = PurchaseUnit(
            unit=pu_raw["unit"],
            typical_package_g=pu_raw.get("typical_package_g"),
            typical_package_ml=pu_raw.get("typical_package_ml"),
        )

    return CanonicalFood(
        id=food_id,
        display_name=display_name,
        allergen_tags=_parse_tag_set(row.get("allergen_tags"), AllergenTag),
        dietary_tags=_parse_tag_set(row.get("dietary_tags"), DietaryTag),
        interaction_tags=_parse_tag_set(row.get("interaction_tags"), InteractionTag),
        aliases=aliases,
        fdc_id=row.get("fdc_id"),
        parent_ids=tuple(row.get("parent_ids") or ()),
        purchase_unit=purchase_unit,
        aisle_tag=row.get("aisle_tag"),
        citations=dict(row.get("citations") or {}),
        notes=row.get("notes", ""),
    )


@lru_cache(maxsize=None)
def get_catalog() -> dict[str, CanonicalFood]:
    """All canonical foods keyed by id. Computed once per process."""
    rows = _load_yaml("canonical_foods")
    if not isinstance(rows, list):
        raise KBError("canonical_foods.yaml root must be a list")
    catalog: dict[str, CanonicalFood] = {}
    for row in rows:
        food = _parse_canonical_food(row)
        if food.id in catalog:
            raise KBError(f"duplicate canonical id: {food.id}")
        catalog[food.id] = food
    return catalog


@lru_cache(maxsize=None)
def get_densities() -> dict[str, dict]:
    data = _load_yaml("densities") or {}
    if not isinstance(data, dict):
        raise KBError("densities.yaml root must be a mapping")
    return data


class AliasIndex:
    """Deterministic alias lookup with exact + fuzzy-prefix matching.

    Built lazily via ``get_alias_index()``. The exact table maps a
    normalized alias → canonical_id. The fuzzy path only fires when
    the exact lookup misses; it walks the index with a simple token-
    overlap score. No ML, no embeddings.
    """

    def __init__(self, by_alias_norm: dict[str, str]) -> None:
        self._exact = by_alias_norm
        # Index by first token for fuzzy search scoping.
        by_first_token: dict[str, list[str]] = {}
        for alias in by_alias_norm:
            tokens = alias.split()
            if not tokens:
                continue
            by_first_token.setdefault(tokens[0], []).append(alias)
        self._by_first_token = by_first_token

    def lookup(self, query: str) -> Optional[AliasMatch]:
        """Return best match for ``query`` (already normalized) or None."""
        if not query:
            return None
        # Exact.
        hit = self._exact.get(query)
        if hit is not None:
            return AliasMatch(canonical_id=hit, score=1.0)
        # Fuzzy: token-overlap Jaccard. Restricted to aliases that
        # share at least one token with the query to keep this cheap.
        q_tokens = set(query.split())
        if not q_tokens:
            return None
        candidates: set[str] = set()
        for t in q_tokens:
            candidates.update(self._by_first_token.get(t, []))
        if not candidates:
            return None
        best: Optional[tuple[str, float]] = None
        for alias in candidates:
            a_tokens = set(alias.split())
            if not a_tokens:
                continue
            overlap = len(q_tokens & a_tokens)
            union = len(q_tokens | a_tokens)
            score = overlap / union
            if best is None or score > best[1]:
                best = (alias, score)
        if best is None:
            return None
        alias, score = best
        if score < 0.5:
            return None
        return AliasMatch(canonical_id=self._exact[alias], score=score)


@lru_cache(maxsize=None)
def get_alias_index() -> AliasIndex:
    catalog = get_catalog()
    table: dict[str, str] = {}
    for food in catalog.values():
        for alias in food.aliases:
            norm = normalize(alias)
            if not norm:
                continue
            existing = table.get(norm)
            if existing is not None and existing != food.id:
                raise KBError(
                    f"alias collision: {alias!r} maps to both {existing!r} and {food.id!r}"
                )
            table[norm] = food.id
    return AliasIndex(table)
