"""Loader for the SPEC-006 shorthand table.

``shorthand.yaml`` maps closed dietary-pattern names (``vegan``,
``keto``, ``gluten-free``, …) to the canonical tag sets they expand
to. The loader validates every tag against the SPEC-005 enums at
import time, so an unknown string in YAML raises here rather than
surfacing as a runtime mystery at resolution time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

import yaml

from ..ingredient_kb.normalizer import normalize
from ..ingredient_kb.taxonomy import AllergenTag, DietaryTag

_DATA_PATH = Path(__file__).resolve().parent / "data" / "shorthand.yaml"


class ShorthandError(ValueError):
    """Raised when ``shorthand.yaml`` fails validation."""


@dataclass(frozen=True)
class ShorthandEntry:
    """One row of ``shorthand.yaml`` with tags coerced to enums."""

    name: str
    forbid_dietary: Tuple[DietaryTag, ...] = ()
    forbid_allergen: Tuple[AllergenTag, ...] = ()
    soft_constraint: Optional[str] = None
    note: str = ""
    synonyms: Tuple[str, ...] = field(default_factory=tuple)


def _coerce_dietary(raw) -> Tuple[DietaryTag, ...]:
    out = []
    for tag in raw or []:
        try:
            out.append(DietaryTag(tag))
        except ValueError as exc:
            raise ShorthandError(f"shorthand.yaml: unknown DietaryTag {tag!r}") from exc
    return tuple(out)


def _coerce_allergen(raw) -> Tuple[AllergenTag, ...]:
    out = []
    for tag in raw or []:
        try:
            out.append(AllergenTag(tag))
        except ValueError as exc:
            raise ShorthandError(f"shorthand.yaml: unknown AllergenTag {tag!r}") from exc
    return tuple(out)


@lru_cache(maxsize=1)
def get_shorthand_index() -> Dict[str, ShorthandEntry]:
    """Return ``{normalized_synonym: ShorthandEntry}``.

    Cached for the life of the process; the YAML is tiny and closed.
    Raises :class:`ShorthandError` on duplicate synonyms or unknown tags.
    """
    if not _DATA_PATH.exists():
        raise ShorthandError(f"shorthand.yaml missing at {_DATA_PATH}")
    with _DATA_PATH.open("r", encoding="utf-8") as fh:
        rows = yaml.safe_load(fh) or []
    if not isinstance(rows, list):
        raise ShorthandError("shorthand.yaml root must be a list")

    index: Dict[str, ShorthandEntry] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ShorthandError(f"shorthand.yaml entry must be a mapping: {row!r}")
        name = row.get("name")
        if not name:
            raise ShorthandError("shorthand.yaml entry missing 'name'")
        entry = ShorthandEntry(
            name=name,
            forbid_dietary=_coerce_dietary(row.get("forbid_dietary")),
            forbid_allergen=_coerce_allergen(row.get("forbid_allergen")),
            soft_constraint=row.get("soft_constraint"),
            note=row.get("note", ""),
            synonyms=tuple(row.get("synonyms") or ()),
        )
        synonyms = list(entry.synonyms) or [entry.name]
        for raw_syn in synonyms:
            key = normalize(raw_syn)
            if not key:
                continue
            if key in index:
                raise ShorthandError(
                    f"shorthand.yaml: duplicate synonym {raw_syn!r} "
                    f"(maps to both {index[key].name!r} and {entry.name!r})"
                )
            index[key] = entry
    return index


def lookup(raw_or_normalized: str) -> Optional[ShorthandEntry]:
    """Return the shorthand entry for a string, or None.

    Accepts either a raw user string (will be normalized) or an
    already-normalized query.
    """
    key = normalize(raw_or_normalized)
    if not key:
        return None
    return get_shorthand_index().get(key)
