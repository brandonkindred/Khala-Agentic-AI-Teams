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


def test_list_changed_files_handles_non_ascii_filename(tmp_path: Path) -> None:
    """``-z`` returns the raw path, so a non-ASCII name is not git-quoted and
    round-trips as a real filesystem path."""
    _init_repo(tmp_path)
    (tmp_path / "café.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "café.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change")

    changed = list_changed_files(tmp_path, "development", "HEAD")

    assert changed == ["café.py"]  # not '"caf\\303\\251.py"'
    assert read_files_as_dict(tmp_path, changed) == {"café.py": "x = 2\n"}


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


def test_read_files_as_dict_skips_paths_outside_repo(tmp_path: Path) -> None:
    """Untrusted keys that escape the repo (``..`` or absolute) are never read."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("inside", encoding="utf-8")
    (tmp_path / "secret.env").write_text("SECRET=1", encoding="utf-8")  # outside repo

    result = read_files_as_dict(repo, ["ok.py", "../secret.env", "/etc/hostname"])

    assert result == {"ok.py": "inside"}  # traversal + absolute paths excluded


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


def test_select_review_input_unions_changed_and_written_from_worktree(tmp_path: Path) -> None:
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
    # The latest pass rewrote a.py and added extra.py on disk (not yet committed).
    (tmp_path / "a.py").write_text("x = 99\n", encoding="utf-8")
    (tmp_path / "extra.py").write_text("z = 0\n", encoding="utf-8")

    task = SimpleNamespace(description="add feature")
    # Only the *keys* of written_files matter; content is read from the worktree.
    files, code = _select_review_input(
        tmp_path, task, written_files={"a.py": "STALE", "extra.py": "STALE"}
    )

    assert code is None
    assert set(files) == {"a.py", "b.py", "extra.py"}  # diff ∪ written paths
    assert files["a.py"] == "x = 99\n"  # worktree content, not the stale dict value
    assert files["extra.py"] == "z = 0\n"  # just-written path, read from disk
    assert files["b.py"] == "y = 3\n"  # diff-only file read from worktree


def test_select_review_input_includes_uncommitted_new_file(tmp_path: Path) -> None:
    """Reviewer's failed-commit scenario: a non-empty committed diff must not
    suppress a newly added file whose commit never landed (but was written)."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "committed change")  # diff is non-empty
    # write_agent_output wrote new.py to disk but its commit failed → uncommitted.
    (tmp_path / "new.py").write_text("n = 1\n", encoding="utf-8")

    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(tmp_path, task, written_files={"new.py": "n = 1\n"})

    assert code is None
    assert "a.py" in files  # committed diff still reviewed
    assert files["new.py"] == "n = 1\n"  # uncommitted new file not dropped


def test_select_review_input_skips_unwritten_paths(tmp_path: Path) -> None:
    """A written_files path that never landed on disk (rejected by write
    validation or a failed write) is excluded — review reflects the branch."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "work")

    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(
        tmp_path, task, written_files={"valid.py": "ok", "rejected.py": "never written"}
    )

    assert code is None
    assert "a.py" in files
    assert "rejected.py" not in files  # absent on disk → excluded
    assert "valid.py" not in files  # also absent on disk → excluded


def test_select_review_input_includes_non_source_changed_files(tmp_path: Path) -> None:
    """An ordinary (non-setup) task that changed requirements.txt / a migration
    has those reviewed — no .py/.java extension filter on the diff."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "requirements.txt").write_text("fastapi\n", encoding="utf-8")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "001_init.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "deps + migration")

    task = SimpleNamespace(description="add dependency")
    files, code = _select_review_input(tmp_path, task, written_files=None)

    assert code is None
    assert "requirements.txt" in files
    assert "migrations/001_init.sql" in files


def test_select_review_input_includes_writer_derived_paths(tmp_path: Path) -> None:
    """The call site passes the writer's normalized output, so derived paths
    (code -> main.py, tests -> tests/test_main.py) are reviewed even when their
    commit didn't land."""
    from software_engineering_team.backend_agent.agent import _select_review_input
    from software_engineering_team.shared.repo_writer import _output_to_files_dict

    _init_repo(tmp_path)
    (tmp_path / "seed.py").write_text("s = 0\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "seed.py").write_text("s = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "committed change")  # non-empty diff

    # write_agent_output would materialize code/tests as main.py and
    # tests/test_main.py; here their commit "failed" so they are only on disk.
    (tmp_path / "main.py").write_text("def f():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_main.py").write_text("def test_f():\n    pass\n", encoding="utf-8")

    output = SimpleNamespace(
        files={}, code="def f():\n    pass\n", tests="def test_f():\n    pass\n", language="python"
    )
    written = _output_to_files_dict(output, "")
    assert {"main.py", "tests/test_main.py"} <= set(written)  # contract we rely on

    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(tmp_path, task, written)

    assert code is None
    assert "main.py" in files
    assert "tests/test_main.py" in files
    assert "seed.py" in files  # committed diff still included


def test_select_review_input_reads_written_paths_when_no_diff(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    # Non-git dir → empty diff → written paths read from the worktree.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(tmp_path, task, written_files={"app/main.py": "ignored"})

    assert code is None
    assert files == {"app/main.py": "print('x')\n"}  # read from worktree


def test_select_review_input_falls_back_to_whole_repo(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    # Non-git dir, no written files → legacy whole-repo code string.
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    task = SimpleNamespace(description="add feature")
    files, code = _select_review_input(tmp_path, task, written_files=None)

    assert files is None
    assert code is not None
    assert "main.py" in code
