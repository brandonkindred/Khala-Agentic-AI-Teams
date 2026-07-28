"""Tests for spec_writing's is_valid coercion (pure functions, no LLM calls)."""

import pytest
from product_requirements_analysis_agent.spec_writing import (
    _coerce_bool,
    _merge_spec_cleanup_results,
    parse_spec_cleanup_response,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("yes", True),
        ("1", True),
        (" true ", True),
        ("false", False),
        ("False", False),
        ("no", False),
        ("0", False),
        ("unrecognized", False),
        ("", False),
        (None, False),
        (1, False),
        (0, False),
        ([], False),
    ],
)
def test_coerce_bool(value, expected) -> None:
    assert _coerce_bool(value) is expected


def test_parse_spec_cleanup_response_string_false_marks_invalid() -> None:
    """Regression test for issue #3361: a string 'false' from the LLM/JSON
    recovery must not be read as a truthy Python bool."""
    raw = {
        "is_valid": "false",
        "validation_issues": ["missing acceptance criteria"],
        "cleaned_spec": "# Spec",
        "summary": "Found issues",
    }
    result = parse_spec_cleanup_response(raw, fallback_spec="# Fallback")
    assert result.is_valid is False
    assert result.validation_issues == ["missing acceptance criteria"]


def test_parse_spec_cleanup_response_string_true() -> None:
    raw = {"is_valid": "true", "cleaned_spec": "# Spec", "summary": "OK"}
    result = parse_spec_cleanup_response(raw, fallback_spec="# Fallback")
    assert result.is_valid is True


def test_parse_spec_cleanup_response_bool_passthrough() -> None:
    raw_true = {"is_valid": True, "cleaned_spec": "# Spec", "summary": "OK"}
    raw_false = {"is_valid": False, "cleaned_spec": "# Spec", "summary": "Bad"}
    assert parse_spec_cleanup_response(raw_true, fallback_spec="# Fallback").is_valid is True
    assert parse_spec_cleanup_response(raw_false, fallback_spec="# Fallback").is_valid is False


def test_parse_spec_cleanup_response_missing_key_defaults_valid() -> None:
    raw = {"cleaned_spec": "# Spec", "summary": "OK"}
    result = parse_spec_cleanup_response(raw, fallback_spec="# Fallback")
    assert result.is_valid is True


def test_parse_spec_cleanup_response_non_dict_falls_back() -> None:
    result = parse_spec_cleanup_response(None, fallback_spec="# Fallback")
    assert result.is_valid is True
    assert result.cleaned_spec == "# Fallback"


def test_merge_spec_cleanup_results_string_false_flips_merged_invalid() -> None:
    """Regression test for issue #3361 in the chunked-merge path: a chunk
    reporting is_valid as the string 'false' must flip the merged result."""
    results = [
        {"is_valid": True, "cleaned_spec": "part 1", "validation_issues": []},
        {"is_valid": "false", "cleaned_spec": "part 2", "validation_issues": ["bad section"]},
    ]
    merged = _merge_spec_cleanup_results(results)
    assert merged["is_valid"] is False
    assert merged["validation_issues"] == ["bad section"]


def test_merge_spec_cleanup_results_all_valid_stays_valid() -> None:
    results = [
        {"is_valid": True, "cleaned_spec": "part 1", "validation_issues": []},
        {"is_valid": "true", "cleaned_spec": "part 2", "validation_issues": []},
    ]
    merged = _merge_spec_cleanup_results(results)
    assert merged["is_valid"] is True
