"""Tests for git-revision previous-content resolution (``code_review_agent.previous_content``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import code_review_agent.previous_content as previous_content
from code_review_agent.previous_content import (
    PreviousContentDiskResult,
    PreviousContentResult,
    read_previous_content_from_git,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit_file(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", f"add {rel}")


def test_alias_previous_content_disk_result_is_shared_type() -> None:
    assert PreviousContentDiskResult is PreviousContentResult


def test_git_hit_returns_blob_text(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "old = 1\n")
    result = read_previous_content_from_git(str(repo), "HEAD", ["a.py"])
    assert isinstance(result, PreviousContentResult)
    assert result.contents == {"a.py": "old = 1\n"}
    assert result.misses == frozenset()


def test_git_miss_for_oversize_blob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(previous_content, "DEFAULT_MAX_FILE_BYTES", 3)
    repo = _init_repo(tmp_path)
    _commit_file(repo, "big.py", "y = 2\n")
    result = read_previous_content_from_git(str(repo), "HEAD", ["big.py"])
    assert "big.py" not in result.contents
    assert result.misses == frozenset({"big.py"})


def test_git_miss_for_absent_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", ["missing.py"])
    assert "missing.py" not in result.contents
    assert result.misses == frozenset({"missing.py"})


def test_fail_open_batch_one_hit_one_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "present.py", "x = 1\n")
    result = read_previous_content_from_git(
        str(repo),
        "HEAD",
        ["present.py", "absent.py"],
    )
    assert result.contents == {"present.py": "x = 1\n"}
    assert result.misses == frozenset({"absent.py"})


def test_no_git_repo_all_misses(tmp_path: Path) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    result = read_previous_content_from_git(str(bare), "HEAD", ["a.py", "b.py"])
    assert result.contents == {}
    assert result.misses == frozenset({"a.py", "b.py"})


def test_bad_revision_all_misses(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "no-such-rev-zzzz", ["a.py"])
    assert result.contents == {}
    assert result.misses == frozenset({"a.py"})


def test_blank_revision_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError):
        read_previous_content_from_git(str(repo), "", ["a.py"])
    with pytest.raises(ValueError):
        read_previous_content_from_git(str(repo), "   ", ["a.py"])


def test_blank_repo_path_raises() -> None:
    with pytest.raises(ValueError):
        read_previous_content_from_git("", "HEAD", ["a.py"])


def test_empty_paths_returns_empty_result(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", [])
    assert result.contents == {}
    assert result.misses == frozenset()


def test_unsafe_path_is_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", ["../secret"])
    assert result.contents == {}
    assert result.misses == frozenset({"../secret"})


def test_blank_path_string_is_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", [""])
    assert result.contents == {}
    assert result.misses == frozenset({""})
