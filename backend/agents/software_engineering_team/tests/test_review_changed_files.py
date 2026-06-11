"""Tests for task-changed-file review input.

Covers the two new shared utilities (``list_changed_files``,
``read_files_as_dict``) and the backend agent's switch from a whole-repo
``code=`` blob to the task's changed files passed as ``files=``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from software_engineering_team.shared.git_utils import list_changed_files
from software_engineering_team.shared.repo_utils import read_files_as_dict


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


# ---------------------------------------------------------------------------
# list_changed_files
# ---------------------------------------------------------------------------


def test_list_changed_files_modified_added_deleted(tmp_path: Path) -> None:
    """Returns added + modified paths on the branch, excludes deletions."""
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "keep.py").write_text("z = 3\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")

    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 99\n", encoding="utf-8")  # modified
    (tmp_path / "c.py").write_text("c = 4\n", encoding="utf-8")  # added
    (tmp_path / "b.py").unlink()  # deleted
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "feature work")

    changed = list_changed_files(tmp_path, "development", "HEAD")

    assert "a.py" in changed
    assert "c.py" in changed
    assert "b.py" not in changed  # deletion excluded by --diff-filter=d
    assert "keep.py" not in changed  # untouched


def test_list_changed_files_non_repo_returns_empty(tmp_path: Path) -> None:
    """A non-git directory yields ``[]`` so callers can fall back."""
    assert list_changed_files(tmp_path, "development", "HEAD") == []


def test_list_changed_files_bad_revision_returns_empty(tmp_path: Path) -> None:
    """A failing git diff (unknown base) yields ``[]`` rather than raising."""
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    assert list_changed_files(tmp_path, "no-such-branch", "HEAD") == []


# ---------------------------------------------------------------------------
# read_files_as_dict
# ---------------------------------------------------------------------------


def test_read_files_as_dict_filters_and_preserves_order(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A", encoding="utf-8")
    (tmp_path / "b.py").write_text("B", encoding="utf-8")
    (tmp_path / "notes.md").write_text("M", encoding="utf-8")

    result = read_files_as_dict(tmp_path, ["b.py", "a.py", "notes.md"], extensions=[".py"])

    assert result == {"b.py": "B", "a.py": "A"}  # .md filtered out
    assert list(result.keys()) == ["b.py", "a.py"]  # input order preserved


def test_read_files_as_dict_no_extension_filter_includes_all(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("fastapi", encoding="utf-8")

    result = read_files_as_dict(tmp_path, ["Dockerfile", "requirements.txt"])

    assert result == {"Dockerfile": "FROM python", "requirements.txt": "fastapi"}


def test_read_files_as_dict_skips_missing_and_undecodable(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A", encoding="utf-8")
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe\x00\x80")  # invalid UTF-8

    result = read_files_as_dict(tmp_path, ["a.py", "missing.py", "bad.py"], extensions=[".py"])

    assert result == {"a.py": "A"}  # missing + undecodable skipped


# ---------------------------------------------------------------------------
# backend BackendExpertAgent._run_code_review passes files= untruncated
# ---------------------------------------------------------------------------


def _task() -> SimpleNamespace:
    return SimpleNamespace(
        description="implement endpoint",
        requirements="must work",
        acceptance_criteria=["c1"],
        user_story=None,
    )


def test_backend_run_code_review_passes_files_untruncated() -> None:
    from software_engineering_team.backend_agent.agent import BackendExpertAgent

    captured: dict = {}

    class _StubAgent:
        def run(self, inp):
            captured["files"] = inp.files
            captured["code"] = inp.code
            return SimpleNamespace(approved=True, issues=[])

    files = {"app/main.py": "print('hi')", "app/util.py": "x = 1"}
    result = BackendExpertAgent._run_code_review(
        code_review_agent=_StubAgent(),
        files=files,
        spec_content="spec",
        task=_task(),
        architecture=None,
    )

    assert result.approved is True
    assert captured["files"] == files  # passed through verbatim, no truncation
    assert captured["code"] == ""  # legacy blob not sent when files present


def test_backend_run_code_review_falls_back_to_code() -> None:
    from software_engineering_team.backend_agent.agent import BackendExpertAgent

    captured: dict = {}

    class _StubAgent:
        def run(self, inp):
            captured["files"] = inp.files
            captured["code"] = inp.code
            return SimpleNamespace(approved=True, issues=[])

    BackendExpertAgent._run_code_review(
        code_review_agent=_StubAgent(),
        code="### a.py ###\nx = 1",
        spec_content="spec",
        task=_task(),
        architecture=None,
    )

    assert captured["files"] is None
    assert captured["code"] == "### a.py ###\nx = 1"


# ---------------------------------------------------------------------------
# _select_review_input fallback chain
# ---------------------------------------------------------------------------


def test_select_review_input_prefers_changed_files(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 3\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "work")

    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(tmp_path, task, written_files={"ignored": "z"})

    assert code is None
    assert set(files) == {"a.py", "b.py"}  # changed files win over written_files


def test_select_review_input_falls_back_to_written_files(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    # Non-git dir → empty diff → written_files used.
    written = {"app/main.py": "print('x')"}
    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(tmp_path, task, written_files=written)

    assert code is None
    assert files == written
    assert files is not written  # returns a copy, not the caller's dict


def test_select_review_input_falls_back_to_whole_repo(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    # Non-git dir, no written files → legacy whole-repo code string.
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(tmp_path, task, written_files=None)

    assert files is None
    assert code is not None
    assert "main.py" in code
