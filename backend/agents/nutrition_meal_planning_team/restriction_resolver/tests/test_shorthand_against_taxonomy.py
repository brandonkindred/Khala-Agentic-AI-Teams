"""SPEC-006 §6.3 — parity check: every tag in shorthand.yaml exists
in the SPEC-005 enums.

Parses the YAML directly (bypassing the loader) so that YAML drift
the loader would mask still trips CI.
"""

from pathlib import Path

import yaml

from nutrition_meal_planning_team.ingredient_kb.taxonomy import (
    AllergenTag,
    DietaryTag,
)

_YAML_PATH = Path(__file__).resolve().parent.parent / "data" / "shorthand.yaml"


def _load_rows():
    with _YAML_PATH.open("r", encoding="utf-8") as fh:
        rows = yaml.safe_load(fh) or []
    assert isinstance(rows, list), "shorthand.yaml root must be a list"
    return rows


def test_every_forbid_dietary_tag_exists_in_enum():
    known = {t.value for t in DietaryTag}
    for row in _load_rows():
        for tag in row.get("forbid_dietary") or []:
            assert tag in known, (
                f"shorthand.yaml: row {row.get('name')!r} has unknown DietaryTag {tag!r}"
            )


def test_every_forbid_allergen_tag_exists_in_enum():
    known = {t.value for t in AllergenTag}
    for row in _load_rows():
        for tag in row.get("forbid_allergen") or []:
            assert tag in known, (
                f"shorthand.yaml: row {row.get('name')!r} has unknown AllergenTag {tag!r}"
            )


def test_no_duplicate_synonyms_across_rows():
    from nutrition_meal_planning_team.ingredient_kb.normalizer import normalize

    seen: dict[str, str] = {}
    for row in _load_rows():
        name = row.get("name")
        for raw_syn in row.get("synonyms") or [name]:
            key = normalize(raw_syn)
            if not key:
                continue
            assert key not in seen, (
                f"shorthand.yaml: synonym {raw_syn!r} duplicated across {seen[key]!r} and {name!r}"
            )
            seen[key] = name


def test_every_synonym_normalizes_non_empty():
    from nutrition_meal_planning_team.ingredient_kb.normalizer import normalize

    for row in _load_rows():
        for raw_syn in row.get("synonyms") or [row.get("name")]:
            assert normalize(raw_syn), (
                f"shorthand.yaml: synonym {raw_syn!r} in {row.get('name')!r} normalizes to empty"
            )
