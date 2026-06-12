"""Tests for task-changed-file review input.

Covers the shared utilities (``list_changed_and_deleted``, ``read_files_as_dict``)
and the backend agent's switch from a whole-repo ``code=`` blob to the task's
changed files passed as ``files=``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from software_engineering_team.shared.git_utils import list_changed_and_deleted
from software_engineering_team.shared.repo_utils import read_files_as_dict


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "commit.gpgsign", "false")


# ---------------------------------------------------------------------------
# list_changed_and_deleted
# ---------------------------------------------------------------------------


def test_list_changed_and_deleted_committed_and_worktree(tmp_path: Path) -> None:
    """Committed + worktree adds/modifies land in `changed`; removals in `deleted`."""
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / "keep.py").write_text("z = 3\n", encoding="utf-8")
    (tmp_path / "wt.py").write_text("w = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")

    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 99\n", encoding="utf-8")  # committed modify
    (tmp_path / "c.py").write_text("c = 4\n", encoding="utf-8")  # committed add
    (tmp_path / "b.py").unlink()  # committed delete
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "feature work")
    # Uncommitted worktree changes:
    (tmp_path / "d.py").write_text("d = 5\n", encoding="utf-8")  # untracked → not changed
    (tmp_path / "keep.py").write_text("z = 99\n", encoding="utf-8")  # tracked modify
    (tmp_path / "wt.py").unlink()  # worktree delete

    changed, deleted = list_changed_and_deleted(tmp_path, "development", "HEAD")

    assert "a.py" in changed and "c.py" in changed  # committed add/modify
    assert "keep.py" in changed  # uncommitted tracked modify
    assert "d.py" not in changed  # untracked excluded
    assert set(deleted) == {"b.py", "wt.py"}  # committed + worktree deletions


def test_list_changed_and_deleted_decomposes_rename(tmp_path: Path) -> None:
    """--no-renames reports the new path as changed and the old path as deleted."""
    _init_repo(tmp_path)
    (tmp_path / "old.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    _git(tmp_path, "mv", "old.py", "new.py")
    _git(tmp_path, "commit", "-m", "rename")

    changed, deleted = list_changed_and_deleted(tmp_path, "development", "HEAD")

    assert "new.py" in changed
    assert "old.py" in deleted


def test_list_changed_and_deleted_non_repo_returns_empty(tmp_path: Path) -> None:
    assert list_changed_and_deleted(tmp_path, "development", "HEAD") == ([], [])


def test_list_changed_and_deleted_bad_revision_degrades(tmp_path: Path) -> None:
    """A failing committed diff still yields the worktree results, no raise."""
    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")  # uncommitted

    changed, deleted = list_changed_and_deleted(tmp_path, "no-such-branch", "HEAD")

    assert "a.py" in changed  # worktree diff still contributes
    assert deleted == []


def test_list_changed_and_deleted_handles_non_ascii_filename(tmp_path: Path) -> None:
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

    changed, _deleted = list_changed_and_deleted(tmp_path, "development", "HEAD")

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


def test_read_files_as_dict_cyclic_symlink_not_fatal(tmp_path: Path) -> None:
    """A cyclic symlink is represented by its target (never dereferenced), so it
    does not abort the read."""
    (tmp_path / "ok.py").write_text("A", encoding="utf-8")
    loop = tmp_path / "loop"
    loop.symlink_to(loop)  # self-referential symlink

    result = read_files_as_dict(tmp_path, ["ok.py", "loop"])

    assert result["ok.py"] == "A"  # sibling still read, no crash
    assert result["loop"].startswith("# symlink ->")  # represented, not dereferenced


def test_read_files_as_dict_represents_symlink_by_target(tmp_path: Path) -> None:
    """A symlink is reported by its link target, never by the target's content."""
    (tmp_path / "real.py").write_text("SECRET = 1\n", encoding="utf-8")
    (tmp_path / "link.py").symlink_to(tmp_path / "real.py")

    result = read_files_as_dict(tmp_path, ["real.py", "link.py"])

    assert result["real.py"] == "SECRET = 1\n"
    assert result["link.py"].startswith("# symlink ->")
    assert "SECRET" not in result["link.py"]  # target content not dereferenced


def test_read_files_as_dict_skips_binary(tmp_path: Path) -> None:
    """Binary content (NUL byte) is omitted rather than decoded into gibberish."""
    (tmp_path / "a.py").write_text("A", encoding="utf-8")
    (tmp_path / "img.png").write_bytes(b"\x89PNG\r\n\x00\x01\x02\x03binary")

    result = read_files_as_dict(tmp_path, ["a.py", "img.png"])

    assert result == {"a.py": "A"}  # binary asset skipped


def test_read_files_as_dict_skips_binary_with_late_nul(tmp_path: Path) -> None:
    """A binary whose first 8 KiB is NUL-free is still detected (whole-file scan)."""
    (tmp_path / "big.bin").write_bytes(b"A" * 9000 + b"\x00" + b"B" * 100)

    result = read_files_as_dict(tmp_path, ["big.bin"])

    assert result == {}  # NUL past the first 8 KiB still flags it as binary


def test_read_files_as_dict_sanitizes_surrogate_key(tmp_path: Path) -> None:
    """A non-UTF-8 filename (read via surrogateescape) yields an encodable key, so
    downstream UTF-8/JSON serialization cannot crash; content is still read."""
    name = "caf\udcff.py"  # lone surrogate, as surrogateescape produces for 0xFF
    (tmp_path / name).write_text("X = 1\n", encoding="utf-8")

    result = read_files_as_dict(tmp_path, [name])

    assert list(result.values()) == ["X = 1\n"]  # content read from disk
    (key,) = result
    key.encode("utf-8")  # must not raise (no lone surrogates)
    assert "\udcff" not in key


def test_read_files_as_dict_skips_missing_preserves_legacy_text(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("A", encoding="utf-8")
    (tmp_path / "legacy.py").write_bytes("café = 1\n".encode("latin-1"))  # non-UTF-8 text

    result = read_files_as_dict(tmp_path, ["a.py", "missing.py", "legacy.py"], extensions=[".py"])

    assert result["a.py"] == "A"
    assert "missing.py" not in result  # missing still skipped
    assert "legacy.py" in result  # legacy-encoded text preserved, not dropped
    assert "= 1" in result["legacy.py"]  # readable remainder survives replacement decode


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


def test_backend_run_code_review_appends_deletion_note_to_description() -> None:
    """The deletion note rides in the task description (review context), not as a
    synthetic source file."""
    from software_engineering_team.backend_agent.agent import BackendExpertAgent

    captured: dict = {}

    class _StubAgent:
        def run(self, inp):
            captured["task_description"] = inp.task_description
            captured["files"] = inp.files
            return SimpleNamespace(approved=True, issues=[])

    BackendExpertAgent._run_code_review(
        code_review_agent=_StubAgent(),
        files={"a.py": "x = 1"},
        deletion_note="Files DELETED by this task:\n- gone.py\n",
        spec_content="spec",
        task=_task(),
        architecture=None,
    )

    assert "implement endpoint" in captured["task_description"]  # original description kept
    assert "gone.py" in captured["task_description"]  # note folded into context
    assert "gone.py" not in captured["files"]  # not injected as a source file


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
    files, code, _note = _select_review_input(
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
    files, code, _note = _select_review_input(tmp_path, task, written_files={"new.py": "n = 1\n"})

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
    files, code, _note = _select_review_input(
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
    files, code, _note = _select_review_input(tmp_path, task, written_files=None)

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
    files, code, _note = _select_review_input(tmp_path, task, written)

    assert code is None
    assert "main.py" in files
    assert "tests/test_main.py" in files
    assert "seed.py" in files  # committed diff still included


def test_select_review_input_includes_uncommitted_worktree_files(tmp_path: Path) -> None:
    """Uncommitted *tracked* changes and writer-owned untracked files are
    reviewed; unrelated untracked leftovers are not."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "committed change")  # non-empty committed diff
    # A failed-commit iteration left these uncommitted on disk:
    (tmp_path / ".gitignore").write_text("*.log\n*.tmp\n", encoding="utf-8")  # modified tracked
    (tmp_path / "untracked.py").write_text("u = 1\n", encoding="utf-8")  # writer-owned untracked
    (tmp_path / "stray.log").write_text("noise\n", encoding="utf-8")  # unrelated leftover

    task = SimpleNamespace(description="add feature")
    files, code, _note = _select_review_input(
        tmp_path, task, written_files={"untracked.py": "u = 1\n"}
    )

    assert code is None
    assert "a.py" in files  # committed diff
    assert ".gitignore" in files  # uncommitted tracked change
    assert "untracked.py" in files  # writer-owned untracked file
    assert "stray.log" not in files  # unrelated leftover excluded


def test_select_review_input_omits_restored_deletion_from_note(tmp_path: Path) -> None:
    """A committed deletion later restored as an uncommitted worktree file is
    reviewed as content, not reported as deleted."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("g = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "gone.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "delete gone")  # committed deletion
    # A later failed-commit iteration restored gone.py (untracked on disk).
    (tmp_path / "gone.py").write_text("g = 2\n", encoding="utf-8")

    task = SimpleNamespace(description="restore gone")
    files, code, note = _select_review_input(tmp_path, task, written_files={"gone.py": "g = 2\n"})

    assert code is None
    assert files["gone.py"] == "g = 2\n"  # reviewed as restored content
    assert note is None  # not reported as deleted


def test_select_review_input_rename_notes_old_reviews_new(tmp_path: Path) -> None:
    """A rename reviews the new path and notes the old path as removed (so callers
    referencing the old import path are flagged)."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "old.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    _git(tmp_path, "mv", "old.py", "new.py")
    _git(tmp_path, "commit", "-m", "rename module")

    task = SimpleNamespace(description="rename module")
    files, code, note = _select_review_input(tmp_path, task, written_files=None)

    assert code is None
    assert "new.py" in files  # destination reviewed as content
    assert note is not None and "old.py" in note  # old path surfaced in the deletion note


def test_select_review_input_notes_deleted_files(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("g = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "gone.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change + delete")

    task = SimpleNamespace(description="add feature")
    files, code, note = _select_review_input(tmp_path, task, written_files=None)

    assert code is None
    assert set(files) == {"a.py"}  # only the real changed file — no synthetic entry
    assert note is not None and "gone.py" in note  # removal surfaced as separate note


def test_select_review_input_deletion_only_returns_note_as_code(tmp_path: Path) -> None:
    """A task whose only change is a deletion reviews the note itself (no
    whole-repo fallback, no synthetic file entry)."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "seed.py").write_text("s = 1\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("g = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "gone.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "delete only")

    task = SimpleNamespace(description="remove module")
    files, code, note = _select_review_input(tmp_path, task, written_files=None)

    assert files is None
    assert note is None
    assert code is not None and "gone.py" in code  # the removal is the review input


def test_select_review_input_note_named_file_reviewed_normally(tmp_path: Path) -> None:
    """A real file named like the old note key is just reviewed as content; the
    deletion note is a separate value, so there is no collision to handle."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "__DELETED_FILES__").write_text("real content\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("g = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "__DELETED_FILES__").write_text("real content v2\n", encoding="utf-8")
    (tmp_path / "gone.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change note-named file + delete")

    task = SimpleNamespace(description="x")
    files, code, note = _select_review_input(tmp_path, task, written_files=None)

    assert code is None
    assert files["__DELETED_FILES__"] == "real content v2\n"  # real file intact
    assert note is not None and "gone.py" in note  # deletion note separate from files


def test_select_review_input_reads_written_paths_when_no_diff(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    # Non-git dir → empty diff → written paths read from the worktree.
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('x')\n", encoding="utf-8")
    task = SimpleNamespace(description="add feature")
    files, code, _note = _select_review_input(tmp_path, task, written_files={"app/main.py": "ignored"})

    assert code is None
    assert files == {"app/main.py": "print('x')\n"}  # read from worktree


def test_select_review_input_falls_back_to_whole_repo(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    # Non-git dir, no written files → legacy whole-repo code string.
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    task = SimpleNamespace(description="add feature")
    files, code, _note = _select_review_input(tmp_path, task, written_files=None)

    assert files is None
    assert code is not None
    assert "main.py" in code


# ---------------------------------------------------------------------------
# _writer_output_keys / _format_deletion_note / build_code_review_input
# ---------------------------------------------------------------------------


def test_writer_output_keys_includes_synthesized_gitignore() -> None:
    """When the output declares gitignore_entries, the synthesized root
    .gitignore (added after the base mapping) is in the reviewed key set."""
    from software_engineering_team.backend_agent.agent import _writer_output_keys

    out = SimpleNamespace(files={"app/main.py": "x = 1"}, gitignore_entries=["*.log"])
    keys = _writer_output_keys(out)

    assert "app/main.py" in keys
    assert ".gitignore" in keys  # synthesized path widened into review


def test_writer_output_keys_no_gitignore_without_entries() -> None:
    from software_engineering_team.backend_agent.agent import _writer_output_keys

    out = SimpleNamespace(files={"app/main.py": "x = 1"}, gitignore_entries=[])
    keys = _writer_output_keys(out)

    assert "app/main.py" in keys
    assert ".gitignore" not in keys


def test_writer_output_keys_none() -> None:
    from software_engineering_team.backend_agent.agent import _writer_output_keys

    assert _writer_output_keys(None) is None


def test_format_deletion_note_sanitizes_surrogate_path() -> None:
    """A deleted path with a lone surrogate is rendered encodable (no crash on
    UTF-8/JSON serialization of the review payload)."""
    from software_engineering_team.backend_agent.agent import _format_deletion_note

    note = _format_deletion_note(["caf\udcff.py"])

    note.encode("utf-8")  # must not raise
    assert "caf" in note
    assert "\udcff" not in note


def test_build_code_review_input_prefers_files() -> None:
    from code_review_agent.models import build_code_review_input

    inp = build_code_review_input(files={"a.py": "x"}, code="ignored", task_description="t")
    assert inp.files == {"a.py": "x"}
    assert inp.code == "ignored"  # forwarded but the model ignores it when files is set


def test_build_code_review_input_code_only() -> None:
    from code_review_agent.models import build_code_review_input

    inp = build_code_review_input(code="### a.py ###\nx", task_description="t")
    assert inp.files is None
    assert inp.code == "### a.py ###\nx"
