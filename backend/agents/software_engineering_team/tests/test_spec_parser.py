"""Tests for spec_parser."""

import pytest
from spec_parser import (
    SPEC_FILENAME,
    get_latest_spec_content,
    get_newest_spec_content,
    get_newest_spec_path,
    get_next_updated_spec_version,
    load_spec_from_repo,
    parse_spec_with_llm,
    validate_repo_path,
    validate_work_path,
)

from llm_service import DummyLLMClient


def test_parse_spec_with_llm_uses_dummy() -> None:
    """LLM parser works with DummyLLMClient."""
    llm = DummyLLMClient()
    spec = "# My Project\n\nDescription here."
    reqs = parse_spec_with_llm(spec, llm)
    assert reqs.title == "Software Project"
    assert isinstance(reqs.acceptance_criteria, list)
    assert reqs.priority == "medium"


def test_parse_spec_with_llm_raises_on_invalid_structure() -> None:
    """When LLM returns acceptance_criteria or constraints as non-list, raises ValueError."""
    from unittest.mock import MagicMock

    mock_llm = MagicMock()
    mock_llm.complete_json.return_value = {
        "title": "Test",
        "description": "Desc",
        "acceptance_criteria": "not a list",  # invalid
        "constraints": [],
        "priority": "medium",
    }
    with pytest.raises(ValueError, match="acceptance_criteria"):
        parse_spec_with_llm("spec", mock_llm)

    mock_llm.complete_json.return_value = {
        "title": "Test",
        "description": "Desc",
        "acceptance_criteria": [],
        "constraints": "not a list",  # invalid
        "priority": "medium",
    }
    with pytest.raises(ValueError, match="constraints"):
        parse_spec_with_llm("spec", mock_llm)


def test_load_spec_from_repo(tmp_path) -> None:
    """load_spec_from_repo reads initial_spec.md from repo root."""
    spec_content = "# Test Project\n\nDescription."
    spec_file = tmp_path / SPEC_FILENAME
    spec_file.write_text(spec_content)

    content = load_spec_from_repo(tmp_path)
    assert content == spec_content


def test_load_spec_from_repo_raises_when_missing(tmp_path) -> None:
    """load_spec_from_repo raises FileNotFoundError when spec missing."""
    import pytest
    with pytest.raises(FileNotFoundError, match="not found"):
        load_spec_from_repo(tmp_path)


def test_validate_repo_path_raises_not_dir(tmp_path) -> None:
    """validate_repo_path raises when path is a file."""
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        validate_repo_path(f)


def test_validate_repo_path_raises_no_git(tmp_path) -> None:
    """validate_repo_path raises when .git missing."""
    (tmp_path / SPEC_FILENAME).write_text("# Project")
    with pytest.raises(ValueError, match="not a git repository"):
        validate_repo_path(tmp_path)


def test_validate_repo_path_raises_no_spec(tmp_path) -> None:
    """validate_repo_path raises when no spec exists anywhere."""
    (tmp_path / ".git").mkdir()  # Minimal git repo marker
    with pytest.raises(ValueError, match="No spec file found"):
        validate_repo_path(tmp_path)


def test_validate_repo_path_success(tmp_path) -> None:
    """validate_repo_path returns resolved path when valid."""
    (tmp_path / ".git").mkdir()
    (tmp_path / SPEC_FILENAME).write_text("# Project")
    result = validate_repo_path(tmp_path)
    assert result == tmp_path.resolve()


def test_validate_repo_path_success_with_only_product_analysis_spec(tmp_path) -> None:
    """validate_repo_path succeeds when only plan/product_analysis/validated_spec.md exists (no root spec)."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "validated_spec.md").write_text("# validated")
    result = validate_repo_path(tmp_path)
    assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# validate_work_path
# ---------------------------------------------------------------------------


def test_validate_work_path_succeeds_with_only_product_analysis_spec(tmp_path) -> None:
    """validate_work_path succeeds when only plan/product_analysis/validated_spec.md exists (no root spec)."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "validated_spec.md").write_text("# validated")
    result = validate_work_path(tmp_path)
    assert result == tmp_path.resolve()


def test_validate_work_path_raises_when_no_spec(tmp_path) -> None:
    """validate_work_path fails when no spec exists anywhere."""
    with pytest.raises(ValueError, match="No spec file found"):
        validate_work_path(tmp_path)


# ---------------------------------------------------------------------------
# get_latest_spec_content
# ---------------------------------------------------------------------------


def test_get_latest_spec_content_prefers_product_analysis_over_plan(tmp_path) -> None:
    """When both plan/product_analysis/validated_spec.md and plan/validated_spec.md exist, content comes from product_analysis."""
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "validated_spec.md").write_text("# plan validated")
    (plan / "product_analysis").mkdir()
    (plan / "product_analysis" / "validated_spec.md").write_text("# product_analysis validated")

    content = get_latest_spec_content(tmp_path)
    assert content == "# product_analysis validated"


def test_get_latest_spec_content_precedence_validated_over_updated(tmp_path) -> None:
    """When both plan/validated_spec.md and plan/updated_spec.md exist, content comes from validated_spec.md."""
    (tmp_path / SPEC_FILENAME).write_text("# initial")
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "updated_spec.md").write_text("# updated")
    (plan / "validated_spec.md").write_text("# validated")

    content = get_latest_spec_content(tmp_path)
    assert content == "# validated"


def test_get_latest_spec_content_versioned_max_n(tmp_path) -> None:
    """When only plan/updated_spec_v1.md and plan/updated_spec_v2.md exist, content comes from v2."""
    (tmp_path / SPEC_FILENAME).write_text("# initial")
    plan = tmp_path / "plan"
    plan.mkdir()
    (plan / "updated_spec_v1.md").write_text("# v1")
    (plan / "updated_spec_v2.md").write_text("# v2")

    content = get_latest_spec_content(tmp_path)
    assert content == "# v2"


def test_get_latest_spec_content_fallback_to_initial_spec(tmp_path) -> None:
    """When no plan files exist, content comes from initial_spec.md at root."""
    (tmp_path / SPEC_FILENAME).write_text("# root initial")

    content = get_latest_spec_content(tmp_path)
    assert content == "# root initial"


def test_get_latest_spec_content_fallback_to_spec_md(tmp_path) -> None:
    """When initial_spec.md is missing but spec.md exists at root, content comes from spec.md."""
    (tmp_path / "spec.md").write_text("# spec.md content")

    content = get_latest_spec_content(tmp_path)
    assert content == "# spec.md content"


def test_get_latest_spec_content_raises_when_none_exist(tmp_path) -> None:
    """get_latest_spec_content raises FileNotFoundError when no candidate spec file exists."""
    with pytest.raises(FileNotFoundError, match="No spec file found"):
        get_latest_spec_content(tmp_path)


# ---------------------------------------------------------------------------
# get_next_updated_spec_version
# ---------------------------------------------------------------------------


def test_get_next_updated_spec_version_no_dir_returns_1(tmp_path) -> None:
    """When plan/product_analysis does not exist, returns 1."""
    assert get_next_updated_spec_version(tmp_path) == 1


def test_get_next_updated_spec_version_empty_dir_returns_1(tmp_path) -> None:
    """When plan/product_analysis exists but has no updated_spec_v*.md, returns 1."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    assert get_next_updated_spec_version(tmp_path) == 1


def test_get_next_updated_spec_version_only_v3_returns_4(tmp_path) -> None:
    """When only updated_spec_v3.md exists in plan/product_analysis, returns 4."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v3.md").write_text("# v3")
    assert get_next_updated_spec_version(tmp_path) == 4


def test_get_next_updated_spec_version_v1_and_v6_returns_7(tmp_path) -> None:
    """When updated_spec_v1.md and updated_spec_v6.md exist, returns 7."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v1.md").write_text("# v1")
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v6.md").write_text("# v6")
    assert get_next_updated_spec_version(tmp_path) == 7


def test_get_next_updated_spec_version_malformed_names_ignored(tmp_path) -> None:
    """Malformed filenames (no numeric suffix or invalid) are ignored; only v2 counts."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v2.md").write_text("# v2")
    (tmp_path / "plan" / "product_analysis" / "updated_spec_vx.md").write_text("# vx")
    (tmp_path / "plan" / "product_analysis" / "updated_spec_v.md").write_text("# v")
    (tmp_path / "plan" / "product_analysis" / "validated_spec.md").write_text("# validated")
    assert get_next_updated_spec_version(tmp_path) == 3


# ---------------------------------------------------------------------------
# get_newest_spec_path / get_newest_spec_content
# ---------------------------------------------------------------------------


def test_get_newest_spec_path_returns_most_recent_by_mtime(tmp_path) -> None:
    """get_newest_spec_path returns the file with the latest mtime among *spec*.md."""
    (tmp_path / "plan" / "product_analysis").mkdir(parents=True)
    older = tmp_path / "plan" / "product_analysis" / "validated_spec.md"
    newer = tmp_path / "plan" / "product_analysis" / "updated_spec_v2.md"
    older.write_text("# validated")
    newer.write_text("# v2")
    import time
    time.sleep(0.02)
    newer.write_text("# v2 updated")
    got = get_newest_spec_path(tmp_path)
    assert got == newer
    assert get_newest_spec_content(tmp_path) == "# v2 updated"


def test_get_newest_spec_path_fallback_when_no_spec_candidates(tmp_path) -> None:
    """When no *spec*.md in plan dirs, falls back to get_latest_spec_path (e.g. initial_spec at root)."""
    (tmp_path / "initial_spec.md").write_text("# root")
    got = get_newest_spec_path(tmp_path)
    assert got == tmp_path / "initial_spec.md"
    assert get_newest_spec_content(tmp_path) == "# root"
