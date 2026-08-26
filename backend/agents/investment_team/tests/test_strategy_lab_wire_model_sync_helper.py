"""Unit tests for assert_wire_fields_subset.

Uses local throwaway Pydantic classes, not the real strategy_lab wire /
persisted pairs — wiring this helper onto the real 7 pairs is tracked
separately (#7279) and is explicitly out of scope here.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ._wire_model_sync_test_helpers import assert_wire_fields_subset


class _PersistedExample(BaseModel):
    a: int
    b: str = ""
    # persisted-only field: must NOT trip the subset check in either direction
    internal_version: int = 1


class _WireExampleMatching(BaseModel):
    a: int
    b: str = ""


class _WireExampleExtraField(BaseModel):
    a: int
    b: str = ""
    c: float = 0.0  # undeclared — not on _PersistedExample, not excluded


class _WireExampleWithExclusion(BaseModel):
    a: int
    b: str = ""
    display_only: str = ""  # deliberately wire-only, documented via exclusions


def test_matching_pair_passes() -> None:
    """Wire fields subset-of persisted fields -> no assertion raised."""
    assert assert_wire_fields_subset(_WireExampleMatching, _PersistedExample) is None


def test_persisted_only_fields_are_not_flagged() -> None:
    """Persisted-only fields (e.g. internal_version) are fine — this is a
    subset check, not an equality check."""
    assert_wire_fields_subset(_WireExampleMatching, _PersistedExample)


def test_extra_undeclared_wire_field_fails_with_clear_message() -> None:
    with pytest.raises(AssertionError) as excinfo:
        assert_wire_fields_subset(_WireExampleExtraField, _PersistedExample)
    message = str(excinfo.value)
    assert "_WireExampleExtraField" in message
    assert "_PersistedExample" in message
    assert "'c'" in message


def test_documented_exclusion_passes() -> None:
    assert_wire_fields_subset(
        _WireExampleWithExclusion,
        _PersistedExample,
        exclusions=("display_only",),
    )


def test_exclusion_that_does_not_cover_all_extra_fields_still_fails() -> None:
    """An exclusion list only silences the fields it names; anything else
    extra still trips the assertion."""
    with pytest.raises(AssertionError, match="c"):
        assert_wire_fields_subset(
            _WireExampleExtraField,
            _PersistedExample,
            exclusions=("some_unrelated_field",),
        )
