"""SPEC-007 W3 — raw YAML schema parity tests.

Loads ``interactions.yaml`` directly (bypassing the loader cache) and
validates structural invariants against the ``Medication`` and
``InteractionTag`` enums. Catches drift the loader's ``lru_cache``
might mask during development.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from agents.nutrition_meal_planning_team.clinical_taxonomy import Medication
from agents.nutrition_meal_planning_team.ingredient_kb.taxonomy import InteractionTag

_YAML_PATH = Path(__file__).resolve().parent.parent / "data" / "interactions.yaml"


def _load_raw() -> dict:
    with open(_YAML_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class TestYamlStructure:
    def test_yaml_file_exists(self) -> None:
        assert _YAML_PATH.exists(), f"interactions.yaml not found at {_YAML_PATH}"

    def test_yaml_root_is_mapping(self) -> None:
        raw = _load_raw()
        assert isinstance(raw, dict)

    def test_every_key_is_known_medication(self) -> None:
        raw = _load_raw()
        med_values = {m.value for m in Medication}
        for key in raw:
            assert key in med_values, f"unknown Medication key in YAML: '{key}'"

    def test_every_medication_enum_has_yaml_entry(self) -> None:
        raw = _load_raw()
        for med in Medication:
            assert med.value in raw, f"Medication.{med.value} missing from interactions.yaml"


class TestTagValidity:
    def test_every_hard_tag_is_known_interaction_tag(self) -> None:
        raw = _load_raw()
        tag_values = {t.value for t in InteractionTag}
        for med, entry in raw.items():
            for tag in entry.get("hard", []):
                assert tag in tag_values, f"unknown InteractionTag '{tag}' in {med}.hard"

    def test_every_flag_tag_is_known_interaction_tag(self) -> None:
        raw = _load_raw()
        tag_values = {t.value for t in InteractionTag}
        for med, entry in raw.items():
            for tag in entry.get("flag", []):
                assert tag in tag_values, f"unknown InteractionTag '{tag}' in {med}.flag"


class TestFieldTypes:
    def test_hard_and_flag_are_lists(self) -> None:
        raw = _load_raw()
        for med, entry in raw.items():
            hard = entry.get("hard", [])
            flag = entry.get("flag", [])
            assert isinstance(hard, list), f"{med}.hard must be a list, got {type(hard).__name__}"
            assert isinstance(flag, list), f"{med}.flag must be a list, got {type(flag).__name__}"

    def test_note_is_string_or_absent(self) -> None:
        raw = _load_raw()
        for med, entry in raw.items():
            if "note" in entry:
                assert isinstance(entry["note"], str), f"{med}.note must be a string"

    def test_hard_and_flag_disjoint(self) -> None:
        raw = _load_raw()
        for med, entry in raw.items():
            hard = set(entry.get("hard", []))
            flag = set(entry.get("flag", []))
            overlap = hard & flag
            assert not overlap, f"{med} has tags in both hard and flag: {overlap}"
