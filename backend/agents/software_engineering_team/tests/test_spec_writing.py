"""Tests for spec_writing's is_valid coercion (pure functions, no LLM calls)."""

from pathlib import Path

import pytest
from product_requirements_analysis_agent.spec_writing import (
    _coerce_bool,
    _merge_spec_cleanup_results,
    _write_spec_artifact,
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
    """Regression: a string 'false' from the LLM/JSON recovery must not be
    read as a truthy Python bool."""
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


@pytest.mark.parametrize(
    "cleaned_spec",
    [None, "", "   ", 42],
)
def test_parse_spec_cleanup_response_invalid_cleaned_spec_uses_fallback(cleaned_spec) -> None:
    raw = {"is_valid": True, "cleaned_spec": cleaned_spec, "summary": "OK"}
    result = parse_spec_cleanup_response(raw, fallback_spec="# Fallback")
    assert result.cleaned_spec == "# Fallback"


def test_parse_spec_cleanup_response_missing_cleaned_spec_uses_fallback() -> None:
    raw = {"is_valid": True, "summary": "OK"}
    result = parse_spec_cleanup_response(raw, fallback_spec="# Fallback")
    assert result.cleaned_spec == "# Fallback"


@pytest.mark.parametrize(
    "summary",
    [None, "", "   ", 7],
)
def test_parse_spec_cleanup_response_invalid_summary_uses_default(summary) -> None:
    raw = {"is_valid": True, "cleaned_spec": "# Spec", "summary": summary}
    result = parse_spec_cleanup_response(raw, fallback_spec="# Fallback")
    assert result.summary == "Spec cleanup complete"


def test_parse_spec_cleanup_response_coerces_validation_issues_to_str() -> None:
    raw = {
        "is_valid": False,
        "validation_issues": ["missing AC", 42, {"code": "gap"}],
        "cleaned_spec": "# Spec",
        "summary": "Found issues",
    }
    result = parse_spec_cleanup_response(raw, fallback_spec="# Fallback")
    assert result.validation_issues == ["missing AC", "42", "{'code': 'gap'}"]


def test_merge_spec_cleanup_results_string_false_flips_merged_invalid() -> None:
    """Regression: a chunk reporting is_valid as the string 'false' must flip
    the merged result."""
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


def test_merge_spec_cleanup_results_accepts_generator() -> None:
    chunks = (
        {"is_valid": True, "cleaned_spec": "part 1", "validation_issues": [1]},
        {"is_valid": False, "cleaned_spec": "part 2", "validation_issues": ["gap"]},
    )
    merged = _merge_spec_cleanup_results(chunks)
    assert merged["is_valid"] is False
    assert merged["cleaned_spec"] == "part 1\n\npart 2"
    assert merged["validation_issues"] == ["1", "gap"]
    assert merged["summary"] == "Cleanup completed for 2 sections"


@pytest.mark.parametrize(
    "filename",
    ["", ".", "..", "../x.md", "a/b.md", "a\\b.md"],
)
def test_write_spec_artifact_rejects_non_bare_filename(tmp_path: Path, filename: str) -> None:
    with pytest.raises(ValueError, match="bare filename"):
        _write_spec_artifact(tmp_path, filename, "# Spec")


def test_write_spec_artifact_writes_versioned_and_latest(tmp_path: Path) -> None:
    out = _write_spec_artifact(tmp_path, "updated_spec_v1.md", "# Spec body")
    plan_dir = tmp_path / "plan" / "product_analysis"
    assert out == plan_dir / "updated_spec_v1.md"
    assert out.read_text(encoding="utf-8") == "# Spec body"
    assert (plan_dir / "updated_spec.md").read_text(encoding="utf-8") == "# Spec body"
