"""Tests for spec_parser."""

import pytest

from spec_parser import (
    get_latest_spec_content,
    load_spec_from_repo,
    parse_spec_with_llm,
    validate_repo_path,
    SPEC_FILENAME,
)
from software_engineering_team.shared.llm import DummyLLMClient


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
    """validate_repo_path raises when initial_spec.md missing."""
    (tmp_path / ".git").mkdir()  # Minimal git repo marker
    with pytest.raises(ValueError, match=SPEC_FILENAME):
        validate_repo_path(tmp_path)


def test_validate_repo_path_success(tmp_path) -> None:
    """validate_repo_path returns resolved path when valid."""
    (tmp_path / ".git").mkdir()
    (tmp_path / SPEC_FILENAME).write_text("# Project")
    result = validate_repo_path(tmp_path)
    assert result == tmp_path.resolve()


# ---------------------------------------------------------------------------
# get_latest_spec_content
# ---------------------------------------------------------------------------


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
