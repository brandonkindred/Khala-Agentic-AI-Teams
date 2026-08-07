"""Tests for aggregating disk/git previous-content results."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from code_review_agent.previous_content import (
    PreviousContentResult,
    merge_previous_content,
    resolve_previous_content,
)


def _result(contents: dict[str, str], misses: set[str]) -> PreviousContentResult:
    return PreviousContentResult(contents=contents, misses=frozenset(misses))


def test_merge_preferred_wins_on_overlap() -> None:
    preferred = _result({"a.py": "from-git\n"}, set())
    fallback = _result({"a.py": "from-disk\n"}, set())
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {"a.py": "from-git\n"}
    assert out.misses == frozenset()


def test_merge_fallback_fills_preferred_miss() -> None:
    preferred = _result({}, {"a.py"})
    fallback = _result({"a.py": "from-disk\n"}, set())
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {"a.py": "from-disk\n"}
    assert out.misses == frozenset()


def test_merge_both_miss_stays_miss() -> None:
    preferred = _result({}, {"a.py"})
    fallback = _result({}, {"a.py"})
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {}
    assert out.misses == frozenset({"a.py"})


def test_merge_empty_preferred_takes_fallback_hits() -> None:
    preferred = _result({}, set())
    fallback = _result({"b.py": "disk\n"}, set())
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {"b.py": "disk\n"}
    assert out.misses == frozenset()


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


def test_resolve_blank_revision_is_disk_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("disk\n", encoding="utf-8")
    for rev in (None, "", "   "):
        out = resolve_previous_content(str(root), ["a.py"], revision=rev)
        assert out.contents == {"a.py": "disk\n"}
        assert out.misses == frozenset()


def test_resolve_git_first_mixed_hit_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    tracked = repo / "tracked.py"
    tracked.write_text("old-git\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "add tracked")
    # Untracked file: git miss, disk hit.
    (repo / "untracked.py").write_text("only-disk\n", encoding="utf-8")
    out = resolve_previous_content(
        str(repo),
        ["tracked.py", "untracked.py", "absent.py"],
        revision="HEAD",
    )
    assert out.contents["tracked.py"] == "old-git\n"
    assert out.contents["untracked.py"] == "only-disk\n"
    assert out.misses == frozenset({"absent.py"})


def test_resolve_both_miss_no_raise(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    out = resolve_previous_content(str(repo), ["missing.py"], revision="HEAD")
    assert out.contents == {}
    assert out.misses == frozenset({"missing.py"})


def test_resolve_blank_repo_path_raises() -> None:
    with pytest.raises(ValueError):
        resolve_previous_content("", ["a.py"], revision=None)
    with pytest.raises(ValueError):
        resolve_previous_content("   ", ["a.py"], revision="HEAD")


def test_resolve_empty_paths(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    out = resolve_previous_content(str(root), [], revision=None)
    assert out.contents == {}
    assert out.misses == frozenset()
