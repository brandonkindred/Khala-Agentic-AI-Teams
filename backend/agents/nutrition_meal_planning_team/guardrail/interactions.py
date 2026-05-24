"""SPEC-007 §4.4 step 4 — medication-interaction policy loader.

Loads ``interactions.yaml`` at first call (cached for process lifetime).
Schema-validated: unknown medication keys or interaction tags fail loudly.
Unknown medication strings from a profile are non-fatal — the caller
gets an empty policy and an advisory list.

Pure function after first load. No I/O, no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Dict, List, Mapping, Tuple

import yaml

from ..clinical_taxonomy import Medication
from ..ingredient_kb.taxonomy import InteractionTag
from .errors import InteractionDataError

_DATA_DIR = Path(__file__).resolve().parent / "data"

_MEDICATION_VALUES = frozenset(m.value for m in Medication)
_INTERACTION_TAG_VALUES = frozenset(t.value for t in InteractionTag)


@dataclass(frozen=True)
class InteractionPolicy:
    """One medication class's forbidden-food-tag policy.

    Preconditions:
        ``medication`` is a ``Medication`` enum value string.
        Every element in ``hard`` and ``flag`` is a valid ``InteractionTag``.
        ``hard`` and ``flag`` are disjoint.

    Postconditions:
        Frozen; safe to hash, cache, and compare by value.

    Invariants:
        ``hard ∩ flag == ∅`` — a tag is either acute or advisory, never both.
    """

    medication: str
    hard: frozenset[InteractionTag]
    flag: frozenset[InteractionTag]
    note: str = ""


EMPTY_POLICY = InteractionPolicy(medication="", hard=frozenset(), flag=frozenset())


def _coerce_tags(raw: list, field_name: str, medication: str) -> frozenset[InteractionTag]:
    """Validate and convert a list of tag strings to a frozenset of InteractionTag.

    Preconditions:
        ``raw`` is a list (possibly empty) of strings.

    Postconditions:
        Returns a frozenset of InteractionTag members.
        Raises InteractionDataError if any string is not a valid InteractionTag.
    """
    if not isinstance(raw, list):
        raise InteractionDataError(
            f"interactions.yaml: {medication}.{field_name} must be a list, got {type(raw).__name__}"
        )
    tags: list[InteractionTag] = []
    for item in raw:
        if not isinstance(item, str):
            raise InteractionDataError(
                f"interactions.yaml: {medication}.{field_name} contains non-string entry: {item!r}"
            )
        if item not in _INTERACTION_TAG_VALUES:
            raise InteractionDataError(
                f"interactions.yaml: unknown InteractionTag '{item}' in {medication}.{field_name}"
            )
        tags.append(InteractionTag(item))
    return frozenset(tags)


@lru_cache(maxsize=1)
def load_interactions() -> Mapping[str, InteractionPolicy]:
    """Load and validate ``interactions.yaml``.

    Preconditions:
        ``data/interactions.yaml`` exists and is valid YAML.

    Postconditions:
        Returns an immutable mapping keyed by every ``Medication`` enum value.
        Each value is a frozen ``InteractionPolicy``.
        Raises ``InteractionDataError`` on any schema violation.

    Invariants:
        Result is identical across calls (cached, YAML is immutable at runtime).
    """
    path = _DATA_DIR / "interactions.yaml"
    if not path.exists():
        raise InteractionDataError(f"interactions.yaml not found at {path}")

    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)

    if not isinstance(raw, dict):
        raise InteractionDataError(
            f"interactions.yaml: root must be a mapping, got {type(raw).__name__}"
        )

    result: dict[str, InteractionPolicy] = {}

    for key, entry in raw.items():
        if key not in _MEDICATION_VALUES:
            raise InteractionDataError(f"interactions.yaml: unknown Medication key '{key}'")
        if not isinstance(entry, dict):
            raise InteractionDataError(
                f"interactions.yaml: {key} must be a mapping, got {type(entry).__name__}"
            )

        hard_raw = entry.get("hard", [])
        flag_raw = entry.get("flag", [])
        note = entry.get("note", "")

        hard = _coerce_tags(hard_raw, "hard", key)
        flag = _coerce_tags(flag_raw, "flag", key)

        overlap = hard & flag
        if overlap:
            tag_names = ", ".join(sorted(t.value for t in overlap))
            raise InteractionDataError(
                f"interactions.yaml: {key} has tags in both hard and flag: {tag_names}"
            )

        if not isinstance(note, str):
            raise InteractionDataError(
                f"interactions.yaml: {key}.note must be a string, got {type(note).__name__}"
            )

        result[key] = InteractionPolicy(medication=key, hard=hard, flag=flag, note=note)

    missing = _MEDICATION_VALUES - set(result.keys())
    if missing:
        raise InteractionDataError(
            f"interactions.yaml: missing entries for Medication members: {sorted(missing)}"
        )

    return MappingProxyType(result)


def get_interaction_policies(
    medications: List[str],
) -> Tuple[Dict[str, InteractionPolicy], List[str]]:
    """Look up interaction policies for a profile's medication list.

    Preconditions:
        ``medications`` is a list of strings (may contain unknown values).

    Postconditions:
        First element: dict of ``{medication_string: InteractionPolicy}``
        for every medication that exists in ``interactions.yaml``.
        Second element: list of medication strings that were not recognized
        (for advisory downstream — never raises on unknowns).
    """
    interactions = load_interactions()
    known: dict[str, InteractionPolicy] = {}
    unknown: list[str] = []
    for med in medications:
        policy = interactions.get(med)
        if policy is not None:
            known[med] = policy
        else:
            unknown.append(med)
    return known, unknown
