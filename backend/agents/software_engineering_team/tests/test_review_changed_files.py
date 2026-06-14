"""Tests for task-changed-file review input.

Covers the shared utilities (``list_changed_and_deleted``, ``read_files_as_dict``)
and the backend agent's switch from a whole-repo ``code=`` blob to the task's
changed files passed as ``files=``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_list_changed_and_deleted_bad_revision_raises(tmp_path: Path) -> None:
    """A missing base ref fails closed (raises) rather than silently degrading."""
    from software_engineering_team.shared.git_utils import BaselineDiffUnavailable

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")

    with pytest.raises(BaselineDiffUnavailable):
        list_changed_and_deleted(tmp_path, "no-such-branch", "HEAD")


def test_list_changed_and_deleted_ambiguous_merge_base_raises(tmp_path: Path, monkeypatch) -> None:
    """Multiple merge bases (criss-cross history) fail closed rather than diffing
    against an arbitrary one."""
    from software_engineering_team.shared import git_utils
    from software_engineering_team.shared.git_utils import BaselineDiffUnavailable

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")

    def fake_run_git(path, cmd, timeout=30, **kwargs):
        if cmd[:2] == ["git", "merge-base"]:
            return 0, "1111111111111111111111111111111111111111\n2222222222222222222222222222222222222222\n"
        # Any further git call (e.g. the diff) means the ambiguity guard did NOT
        # fire — fail loudly so the test can't pass for the wrong reason.
        raise AssertionError(f"unexpected git command after ambiguous merge base: {cmd}")

    monkeypatch.setattr(git_utils, "_run_git", fake_run_git)

    with pytest.raises(BaselineDiffUnavailable):
        list_changed_and_deleted(tmp_path, "development", "HEAD")


def test_list_changed_and_deleted_net_add_then_delete_excluded(tmp_path: Path) -> None:
    """A path added in a feature commit and then deleted in the worktree has no
    net change vs base, so it appears in neither list."""
    _init_repo(tmp_path)
    (tmp_path / "seed.py").write_text("s = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "temp.py").write_text("t = 1\n", encoding="utf-8")  # added in commit
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add temp")
    (tmp_path / "temp.py").unlink()  # removed in worktree → net no change vs base

    changed, deleted = list_changed_and_deleted(tmp_path, "development", "HEAD")

    assert "temp.py" not in changed
    assert "temp.py" not in deleted  # no spurious deletion for a base-absent file


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
    try:
        loop.symlink_to(loop)  # self-referential symlink
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not supported on this platform")

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


def test_read_files_as_dict_passes_large_file_untruncated(tmp_path: Path) -> None:
    """A large text file is passed whole — the coordinator segments it itself, so
    nothing in the tail is dropped from review."""
    big = "A" * 2_000_000 + "TAIL_MARKER\n"
    (tmp_path / "big.py").write_text(big, encoding="utf-8")

    result = read_files_as_dict(tmp_path, ["big.py"])

    content = result["big.py"]
    assert content == big  # full content, byte-for-byte
    assert "TAIL_MARKER" in content  # the tail is present, not truncated
    assert "review-truncated" not in content  # no lossy truncation marker


def test_read_repo_files_as_dict_all_types_excludes_build_and_secrets(tmp_path: Path) -> None:
    """The whole-repo reader covers all text types, skipping build dirs + secrets."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    (tmp_path / "schema.sql").write_text("CREATE TABLE t (id int);\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")  # secret → excluded
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x\n", encoding="utf-8")  # build → excluded

    result = read_repo_files_as_dict(tmp_path)

    assert "main.py" in result
    assert "config.yaml" in result  # non-.py types included
    assert "schema.sql" in result
    assert ".env" not in result  # secret excluded
    assert "node_modules/dep.js" not in result  # build dir excluded


def test_read_files_as_dict_skips_binary_with_nul_after_text_prefix(tmp_path: Path) -> None:
    """A binary with a NUL only after a leading text run is still detected."""
    (tmp_path / "big.bin").write_bytes(b"A" * 9000 + b"\x00" + b"B" * 100)

    result = read_files_as_dict(tmp_path, ["big.bin"])

    assert result == {}  # the NUL after the text prefix flags it as binary


def test_read_files_as_dict_sanitizes_surrogate_key(tmp_path: Path) -> None:
    """A non-UTF-8 filename (read via surrogateescape) yields an encodable key, so
    downstream UTF-8/JSON serialization cannot crash; content is still read."""
    name = "caf\udcff.py"  # lone surrogate, as surrogateescape produces for 0xFF
    try:
        (tmp_path / name).write_text("X = 1\n", encoding="utf-8")
    except (OSError, UnicodeEncodeError):
        pytest.skip("filesystem cannot represent a lone-surrogate filename")

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


def test_backend_run_code_review_keeps_task_description_clean() -> None:
    """``_run_code_review`` forwards the task description verbatim — the deletion
    note is folded into the segmented files channel upstream, never appended to
    the description (which the coordinator repeats unsegmented per chunk)."""
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
        spec_content="spec",
        task=_task(),
        architecture=None,
    )

    assert captured["task_description"] == "implement endpoint"  # verbatim, nothing appended


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
    from software_engineering_team.backend_agent.agent import (
        _DELETION_NOTE_PATH,
        _select_review_input,
    )

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
    assert "a.py" in files  # the real changed file is reviewed
    # The deletion note rides the segmented files channel under the synthetic key,
    # not the per-chunk-repeated task description.
    assert _DELETION_NOTE_PATH in files and "gone.py" in files[_DELETION_NOTE_PATH]
    assert note is not None and "gone.py" in note  # also returned for inspection


def test_select_review_input_deletion_only_reviews_note_as_block(tmp_path: Path) -> None:
    """A deletion-only change carries the note as the sole entry in the segmented
    files channel (an empty input would make run_coordinator auto-approve)."""
    from software_engineering_team.backend_agent.agent import (
        _DELETION_NOTE_PATH,
        _select_review_input,
    )

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

    assert code is None  # not the legacy blob channel
    assert files is not None
    # The note is the one segmentable block — non-empty so the model actually runs.
    assert "gone.py" in files[_DELETION_NOTE_PATH]
    assert note and "gone.py" in note  # also returned for logging/inspection


def test_select_review_input_note_key_collision_keeps_real_file(tmp_path: Path) -> None:
    """A real changed file named like the note key is not overwritten by the note;
    the note moves to a disambiguated key so both are reviewed."""
    from software_engineering_team.backend_agent.agent import (
        _DELETION_NOTE_PATH,
        _select_review_input,
    )

    _init_repo(tmp_path)
    # A real file whose repo-relative path equals the synthetic note key.
    note_named = tmp_path / _DELETION_NOTE_PATH
    note_named.parent.mkdir(parents=True, exist_ok=True)
    note_named.write_text("real = 1\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("g = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    note_named.write_text("real = 2\n", encoding="utf-8")  # real change
    (tmp_path / "gone.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change note-named file + delete")

    task = SimpleNamespace(description="x")
    files, _code, note = _select_review_input(tmp_path, task, written_files=None)

    # The real file keeps the note key and its own content; the deletion note is
    # relocated to a distinct, suffixed key so neither clobbers the other.
    assert files[_DELETION_NOTE_PATH] == "real = 2\n"  # real file content preserved
    note_keys = [k for k, v in files.items() if k != _DELETION_NOTE_PATH and note in v]
    assert note_keys and _DELETION_NOTE_PATH not in note_keys  # note under a distinct key
    assert note and "gone.py" in note


def test_select_review_input_notes_file_replaced_by_directory(tmp_path: Path) -> None:
    """A file deleted and replaced by a directory at the same path is still noted
    deleted (the directory does not count as the file being 'present')."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "pkg").write_text("p = 1\n", encoding="utf-8")  # tracked file
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "pkg").unlink()
    (tmp_path / "pkg").mkdir()  # same path now a directory
    (tmp_path / "pkg" / "main.py").write_text("m = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "file -> package")

    task = SimpleNamespace(description="x")
    files, _code, note = _select_review_input(tmp_path, task, written_files=None)

    assert "pkg/main.py" in files  # the new child is reviewed
    assert note is not None and "- pkg\n" in note  # the removed file is still surfaced


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

    # Non-git dir, no written files → whole-repo fallback (all reviewable files).
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("a: 1\n", encoding="utf-8")
    task = SimpleNamespace(description="add feature")
    files, code, _note = _select_review_input(tmp_path, task, written_files=None)

    assert code is None
    assert "main.py" in files
    assert "config.yaml" in files  # non-.py file types reviewed in the fallback


def test_select_review_input_excludes_sensitive_files(tmp_path: Path) -> None:
    """A changed secret (.env, key) is not forwarded as content, but its path is
    surfaced in a 'withheld' note so the change cannot silently bypass the gate."""
    from software_engineering_team.backend_agent.agent import (
        _WITHHELD_NOTE_PATH,
        _select_review_input,
    )

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=base\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=leaked\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change + touch env")

    task = SimpleNamespace(description="x")
    files, code, _note = _select_review_input(tmp_path, task, written_files=None)

    assert code is None
    assert "a.py" in files
    assert ".env" not in files  # secret content not in the review payload
    # The path is surfaced for manual review; the secret VALUE never is.
    assert ".env" in files[_WITHHELD_NOTE_PATH]
    assert "SECRET=leaked" not in files[_WITHHELD_NOTE_PATH]


def test_select_review_input_withheld_only_change_is_reviewable(tmp_path: Path) -> None:
    """A task that changes ONLY a sensitive-named file still produces a non-empty
    review input (the withheld note), so it cannot auto-approve via an empty set."""
    from software_engineering_team.backend_agent.agent import (
        _WITHHELD_NOTE_PATH,
        _select_review_input,
    )

    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "secrets.py").write_text("TOKEN = 'base'\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "app" / "secrets.py").write_text("TOKEN = 'changed'\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change secrets.py")

    task = SimpleNamespace(description="x")
    files, code, _note = _select_review_input(tmp_path, task, written_files=None)

    # Without the withheld note this would be an empty diff → whole-repo fallback
    # could approve; the note keeps the sensitive-named code change visible.
    assert code is None
    assert "app/secrets.py" in files[_WITHHELD_NOTE_PATH]
    assert "TOKEN = 'changed'" not in files[_WITHHELD_NOTE_PATH]


def test_select_review_input_deleted_secret_path_surfaced_without_content(
    tmp_path: Path,
) -> None:
    """A deleted secret's CONTENT is withheld, but its PATH is surfaced (in the
    withheld note) so a deletion-only secret change is not silently approved."""
    from software_engineering_team.backend_agent.agent import (
        _WITHHELD_NOTE_PATH,
        _select_review_input,
    )

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=base\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("g = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / ".env").unlink()  # deleted secret
    (tmp_path / "gone.py").unlink()  # ordinary deletion
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "delete env + gone")

    task = SimpleNamespace(description="x")
    files, _code, note = _select_review_input(tmp_path, task, written_files=None)

    assert note is not None
    assert "gone.py" in note  # ordinary deletion surfaced in the deletion note
    assert ".env" not in note  # secret stays OUT of the deletion note
    # ...but the secret deletion is surfaced (path only) in the withheld note.
    assert ".env" in files[_WITHHELD_NOTE_PATH]
    assert "SECRET=base" not in files[_WITHHELD_NOTE_PATH]  # never its content


def test_select_review_input_fails_closed_to_whole_repo(tmp_path: Path) -> None:
    """When the baseline diff can't be computed, review the whole repo (closed)."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    # No `development` branch exists → merge-base fails → fail closed.

    task = SimpleNamespace(description="x")
    # written_files present, but the partial set must NOT be used on baseline
    # failure — the fail-closed path reviews the whole repo from disk instead.
    files, code, _note = _select_review_input(tmp_path, task, written_files={"main.py": "x"})

    assert code is None
    # Content proves it is the whole-repo on-disk read, not the written_files dict
    # (which would have been the placeholder "x").
    assert files["main.py"] == "print('hi')\n"


def test_select_review_input_deletion_note_lists_every_path(tmp_path: Path) -> None:
    """Every deleted path is listed (fail-closed coverage), not hidden behind a
    count — the reviewer must be able to inspect each removal — and the full list
    travels in the segmented files channel, not the per-chunk task description."""
    from software_engineering_team.backend_agent.agent import (
        _DELETION_NOTE_PATH,
        _select_review_input,
    )

    total = 120
    _init_repo(tmp_path)
    (tmp_path / "keep.py").write_text("k = 1\n", encoding="utf-8")
    for i in range(total):
        (tmp_path / f"gone{i}.py").write_text("g\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "keep.py").write_text("k = 2\n", encoding="utf-8")
    for i in range(total):
        (tmp_path / f"gone{i}.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "mass delete")

    task = SimpleNamespace(description="x")
    files, _code, note = _select_review_input(tmp_path, task, written_files=None)

    assert note is not None
    assert f"{total} total" in note  # full count preserved
    assert "more" not in note  # nothing hidden behind an "... and N more" summary
    for i in range(total):
        assert f"gone{i}.py" in note  # every removed path is individually listed
    assert note.count("- gone") == total  # all listed, none collapsed
    # The list is carried in the segmented files channel (the coordinator chunks
    # blocks), never appended to a per-chunk-repeated task description.
    assert files[_DELETION_NOTE_PATH] == note


def test_select_review_input_restored_dangling_symlink_not_deleted(tmp_path: Path) -> None:
    """A committed deletion restored as a dangling symlink is not noted deleted."""
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
    _git(tmp_path, "commit", "-m", "delete gone")
    # Restore gone.py as a dangling symlink (target does not exist).
    (tmp_path / "gone.py").symlink_to("nonexistent-target")

    task = SimpleNamespace(description="x")
    files, _code, note = _select_review_input(tmp_path, task, written_files={"gone.py": "x"})

    assert "gone.py" in files  # reviewed as the restored symlink marker
    assert note is None or "gone.py" not in note  # not contradictorily noted deleted


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


def test_strip_surrogates_is_injective() -> None:
    """Distinct invalid byte sequences map to distinct, encodable strings, so two
    such filenames do not collide to one review key."""
    from software_engineering_team.shared.repo_utils import strip_surrogates

    a = strip_surrogates("a\udcff.py")
    b = strip_surrogates("a\udcfe.py")

    assert a != b  # injective: different bad bytes → different keys
    a.encode("utf-8")  # both must be UTF-8 encodable
    b.encode("utf-8")
    assert strip_surrogates("plain.py") == "plain.py"  # ascii unchanged


def test_strip_surrogates_leaves_ordinary_backslash_paths_intact() -> None:
    """A valid path containing a literal backslash is not rewritten — preserving
    it matters more than the purely theoretical surrogate-vs-literal collision, so
    the review-map key keeps matching the real on-disk file."""
    from software_engineering_team.shared.repo_utils import strip_surrogates

    literal = strip_surrogates("a\\udcff.py")  # backslash-u-d-c-f-f, a real filename
    assert literal == "a\\udcff.py"  # unchanged, single backslash
    literal.encode("utf-8")  # still encodable


def test_is_sensitive_path() -> None:
    from software_engineering_team.shared.repo_utils import is_sensitive_path

    # Secrets — excluded (basename, anchored .env, key suffix)
    assert is_sensitive_path(".env")
    assert is_sensitive_path("config/.env.production")
    assert is_sensitive_path(".envrc")  # direnv often holds `export SECRET=`
    assert is_sensitive_path("deploy/server.pem")
    assert is_sensitive_path("secrets/id_rsa")
    # Secret directory component anywhere in the path
    assert is_sensitive_path("secrets/config.json")
    assert is_sensitive_path("app/credentials/token.txt")
    assert is_sensitive_path("home/.ssh/known_hosts")
    # Secret stem (stem + extension forms)
    assert is_sensitive_path("config/credentials.json")
    assert is_sensitive_path("app/secrets.py")
    # Capitalized variants — case cannot bypass the filter (case-sensitive FS)
    assert is_sensitive_path(".ENV")
    assert is_sensitive_path("config/.Env.Production")
    assert is_sensitive_path("secrets/ID_RSA")
    assert is_sensitive_path("deploy/server.PEM")
    assert is_sensitive_path("app/.SSH/config")
    assert is_sensitive_path("config/Credentials.JSON")
    # Ordinary source — NOT excluded (anchored .env, no `.env` prefix over-match,
    # no stem over-match)
    assert not is_sensitive_path(".environment.py")
    assert not is_sensitive_path("env.py")
    assert not is_sensitive_path("environment.py")
    assert not is_sensitive_path("app/secret_manager.py")  # stem != "secret(s)"
    assert not is_sensitive_path("app/main.py")
    assert not is_sensitive_path("requirements.txt")


def test_read_files_as_dict_distinct_surrogate_paths_do_not_collide(tmp_path: Path) -> None:
    """Two changed files differing only in invalid bytes both survive into the map."""
    n1, n2 = "a\udcff.py", "a\udcfe.py"
    (tmp_path / n1).write_text("X1\n", encoding="utf-8")
    (tmp_path / n2).write_text("X2\n", encoding="utf-8")

    result = read_files_as_dict(tmp_path, [n1, n2])

    assert len(result) == 2  # neither overwrote the other
    assert sorted(result.values()) == ["X1\n", "X2\n"]


def test_read_files_as_dict_surrogate_and_literal_backslash_collision(tmp_path: Path) -> None:
    """A surrogate-escaped name and a literal-backslash name that sanitize to the
    same key are both reviewed — the second is disambiguated, never overwritten."""
    surrogate = "a\udcff.py"  # invalid 0xFF byte → sanitizes to 'a\\udcff.py'
    literal = "a\\udcff.py"  # real backslashes → sanitizes to the same 'a\\udcff.py'
    (tmp_path / surrogate).write_text("FROM_BYTE\n", encoding="utf-8")
    (tmp_path / literal).write_text("FROM_LITERAL\n", encoding="utf-8")

    result = read_files_as_dict(tmp_path, [surrogate, literal])

    assert len(result) == 2  # both survive; neither silently dropped
    assert sorted(result.values()) == ["FROM_BYTE\n", "FROM_LITERAL\n"]
    assert all(k.encode("utf-8") for k in result)  # keys still UTF-8 encodable


def test_strip_surrogates_preserves_literal_backslash() -> None:
    """An ordinary character — including a literal backslash in a valid POSIX
    filename — is left untouched; only lone surrogates are escaped."""
    from software_engineering_team.shared.repo_utils import strip_surrogates

    # A real backslash in a path must round-trip unchanged so the review-map key
    # still matches the on-disk file for downstream fix logic.
    assert strip_surrogates("a\\b.py") == "a\\b.py"
    assert strip_surrogates("dir/we\\ird.py") == "dir/we\\ird.py"
    # A lone surrogate (from surrogateescape) is still escaped to stay encodable.
    out = strip_surrogates("caf\udcff.py")
    out.encode("utf-8")  # must not raise
    assert "\udcff" not in out


def test_read_repo_files_as_dict_repo_under_excluded_dir_name(tmp_path: Path) -> None:
    """When the repo root itself sits under a dir named like an excluded one
    (``node_modules``), files are still read — exclusion is repo-relative, so the
    fallback does not degrade to an empty (trivially approved) review."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    repo = tmp_path / "node_modules" / "myproject"
    repo.mkdir(parents=True)
    (repo / "main.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "util.py").write_text("y = 2\n", encoding="utf-8")
    # A genuinely-nested excluded dir inside the repo is still skipped.
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("z\n", encoding="utf-8")

    result = read_repo_files_as_dict(repo)

    assert "main.py" in result  # ancestor named node_modules does not exclude it
    assert "pkg/util.py" in result
    assert "node_modules/dep.js" not in result  # repo-relative exclusion still applies


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


# ---------------------------------------------------------------------------
# Never-silently-skip review coverage: deleted content, emptied, unreadable,
# control-char escaping, synthetic-note routing, venv exclusion
# ---------------------------------------------------------------------------


def test_select_review_input_includes_deleted_file_content(tmp_path: Path) -> None:
    """The pre-deletion content of a removed file is read from the merge base and
    surfaced under its real path, so the reviewer can inspect the removed logic."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "module.py").write_text("def critical():\n    return 42\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "module.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "delete module")

    task = SimpleNamespace(description="x")
    sel = _select_review_input(tmp_path, task, written_files=None)
    files, code, note = sel

    assert code is None
    # The blob is byte-for-byte (no header), under a DELETED-marked display label
    # so every segment carries the deletion context; key_to_path maps the label
    # back to the real removed path for finding-driven restoration.
    del_keys = [k for k in files if "module.py" in k and "DELETED" in k]
    assert len(del_keys) == 1
    assert files[del_keys[0]] == "def critical():\n    return 42\n"  # byte-for-byte
    assert sel.key_to_path[del_keys[0]] == "module.py"
    assert note is not None and "DELETED" in note and "module.py" in note


def test_select_review_input_deleted_binary_listed_without_content(tmp_path: Path) -> None:
    """A deleted binary file has no readable content block, but is still listed in
    the deletion note (never silently dropped)."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "img.bin").write_bytes(b"\x00\x01\x02BIN\x00")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "img.bin").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "delete binary")

    task = SimpleNamespace(description="x")
    files, _code, note = _select_review_input(tmp_path, task, written_files=None)

    assert "img.bin" not in files  # binary content not added as a block
    assert note is not None and "img.bin" in note  # but still surfaced by name


def test_select_review_input_flags_emptied_file(tmp_path: Path) -> None:
    """A changed file truncated to whitespace-only is flagged so the destructive
    truncation is reviewed instead of auto-approved on an empty block."""
    from software_engineering_team.backend_agent.agent import (
        _EMPTIED_NOTE_PATH,
        _select_review_input,
    )

    _init_repo(tmp_path)
    (tmp_path / "important.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "important.py").write_text("   \n\n", encoding="utf-8")  # truncated
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "truncate")

    task = SimpleNamespace(description="x")
    files, _code, _note = _select_review_input(tmp_path, task, written_files=None)

    assert _EMPTIED_NOTE_PATH in files
    assert "important.py" in files[_EMPTIED_NOTE_PATH]


def test_select_review_input_flags_unreadable_changed_file(tmp_path: Path) -> None:
    """A changed file that becomes unreadable (here: a binary blob) is reported in
    an 'unreadable' note rather than being silently dropped from a partial review."""
    from software_engineering_team.backend_agent.agent import (
        _UNREADABLE_NOTE_PATH,
        _select_review_input,
    )

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "data.dat").write_text("text\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "data.dat").write_bytes(b"now\x00binary")  # changed to binary
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "binarize")

    task = SimpleNamespace(description="x")
    files, _code, _note = _select_review_input(tmp_path, task, written_files=None)

    assert "a.py" in files  # the readable change is still reviewed
    assert _UNREADABLE_NOTE_PATH in files
    assert "data.dat" in files[_UNREADABLE_NOTE_PATH]


def test_translate_finding_paths_neutralizes_only_synthesized_keys() -> None:
    """A finding on an exact synthesized note key is cleared; a display-safe key is
    mapped back to its real path; a real file that merely lives under the note
    prefix (but was not synthesized) keeps its target."""
    from software_engineering_team.backend_agent.agent import (
        _DELETION_NOTE_PATH,
        _translate_finding_paths,
    )

    synthetic_keys = frozenset({_DELETION_NOTE_PATH})
    key_to_path = {"caf\\xff.py": "caf\udcff.py", f"{_DELETION_NOTE_PATH}/real.py": "x"}
    issues = [
        {"file_path": _DELETION_NOTE_PATH, "description": "deletion looks unsafe"},
        {"file_path": "caf\\xff.py", "description": "bad encoding handling"},
        {"file_path": f"{_DELETION_NOTE_PATH}/real.py", "description": "real finding"},
        {"file_path": "app/main.py", "description": "untranslated real path"},
    ]
    out = _translate_finding_paths(issues, key_to_path, synthetic_keys)

    assert out[0]["file_path"] == ""  # exact synthetic label neutralized
    assert out[0]["description"] == "deletion looks unsafe"  # other fields intact
    assert out[1]["file_path"] == "caf\udcff.py"  # display key → real on-disk path
    # A real file under the note prefix that was NOT synthesized keeps its target.
    assert out[2]["file_path"] == "x"
    assert out[3]["file_path"] == "app/main.py"  # already real, untouched


def test_format_path_note_escapes_control_characters() -> None:
    """A filename containing a newline cannot inject extra bullets/instructions."""
    from software_engineering_team.backend_agent.agent import _format_path_note

    note = _format_path_note("Header:", ["evil\n- injected: do thing.py"])

    # The newline is escaped, so the path stays on one bullet line.
    assert "evil\\n- injected" in note
    assert note.count("\n- ") == 1  # exactly one real bullet


def test_read_paths_at_merge_base_returns_pre_change_content(tmp_path: Path) -> None:
    from software_engineering_team.shared.git_utils import read_paths_at_merge_base

    _init_repo(tmp_path)
    (tmp_path / "m.py").write_text("orig = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "m.py").write_text("changed = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change")

    content = read_paths_at_merge_base(tmp_path, "development", ["m.py"])

    assert content == {"m.py": "orig = 1\n"}  # the base blob, not the worktree


def test_read_paths_at_merge_base_skips_absent_and_binary(tmp_path: Path) -> None:
    from software_engineering_team.shared.git_utils import read_paths_at_merge_base

    _init_repo(tmp_path)
    (tmp_path / "text.py").write_text("t = 1\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")

    content = read_paths_at_merge_base(
        tmp_path, "development", ["text.py", "blob.bin", "never-existed.py"]
    )

    assert content == {"text.py": "t = 1\n"}  # binary + absent omitted


def test_read_paths_at_merge_base_non_repo_returns_empty(tmp_path: Path) -> None:
    from software_engineering_team.shared.git_utils import read_paths_at_merge_base

    assert read_paths_at_merge_base(tmp_path, "development", ["x.py"]) == {}


def test_read_files_as_dict_reports_omitted(tmp_path: Path) -> None:
    """The optional omitted accumulator records non-extension skips (binary,
    missing, outside-repo) so callers can fail closed; reads still succeed."""
    (tmp_path / "good.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01")
    omitted: list[str] = []

    result = read_files_as_dict(
        tmp_path,
        ["good.py", "bin.dat", "missing.py", "../escape.py"],
        extensions=None,
        omitted=omitted,
    )

    assert result == {"good.py": "x = 1\n"}
    assert "bin.dat" in omitted  # binary
    assert "missing.py" in omitted  # vanished/unreadable
    assert "../escape.py" in omitted  # outside repo


def test_read_files_as_dict_omitted_ignores_extension_filter(tmp_path: Path) -> None:
    """An extension-filtered path is a deliberate caller scoping choice, not an
    omission, so it is not reported in the accumulator."""
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "skip.md").write_text("# doc\n", encoding="utf-8")
    omitted: list[str] = []

    result = read_files_as_dict(
        tmp_path, ["keep.py", "skip.md"], extensions=[".py"], omitted=omitted
    )

    assert result == {"keep.py": "x = 1\n"}
    assert omitted == []  # extension filter is not an omission


def test_read_repo_files_as_dict_excludes_virtualenv(tmp_path: Path) -> None:
    """The whole-repo fallback skips venv/.venv/__pycache__ so a local virtual
    environment does not flood the review with dependency files."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    for d in (".venv", "venv", "__pycache__"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "dep.py").write_text("junk = 1\n", encoding="utf-8")

    result = read_repo_files_as_dict(tmp_path)

    assert "main.py" in result
    assert not any(k.startswith((".venv/", "venv/", "__pycache__/")) for k in result)


def test_sanitize_path_for_text_escapes_controls_keeps_unicode() -> None:
    from software_engineering_team.shared.repo_utils import sanitize_path_for_text

    assert sanitize_path_for_text("a\nb\tc.py") == "a\\nb\\tc.py"
    assert sanitize_path_for_text("café.py") == "café.py"  # printable unicode kept
    # Lone surrogate is made encodable and the result has no control chars.
    out = sanitize_path_for_text("x\udcff\x07.py")
    out.encode("utf-8")  # must not raise
    assert "\x07" not in out


# ---------------------------------------------------------------------------
# Reverse references, note-only gating, key→path mapping
# ---------------------------------------------------------------------------


def test_find_referencing_paths_lists_surviving_importers(tmp_path: Path) -> None:
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text("from pkg.helper import h\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    # Delete the helper from the worktree (consumer still imports it).
    (tmp_path / "pkg" / "helper.py").unlink()

    refs = find_referencing_paths(tmp_path, ["pkg/helper.py"])

    assert "consumer.py" in refs["pkg/helper.py"]  # surviving importer surfaced
    assert "unrelated.py" not in refs.get("pkg/helper.py", [])


def test_find_referencing_paths_skips_non_python_and_non_repo(tmp_path: Path) -> None:
    from software_engineering_team.shared.git_utils import find_referencing_paths

    assert find_referencing_paths(tmp_path, ["a.py"]) == {}  # non-repo → {}
    _init_repo(tmp_path)
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    assert find_referencing_paths(tmp_path, ["data.bin"]) == {}  # non-.py skipped


def test_find_referencing_paths_strips_source_root(tmp_path: Path) -> None:
    """A module under a src/ root is imported by its package path (pkg.helper),
    not the on-disk path (src.pkg.helper) — the scan strips the source root."""
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg" / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text("from pkg.helper import h\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "src" / "pkg" / "helper.py").unlink()

    refs = find_referencing_paths(tmp_path, ["src/pkg/helper.py"])

    assert "consumer.py" in refs["src/pkg/helper.py"]  # matched on pkg.helper


def test_find_referencing_paths_init_uses_package_name(tmp_path: Path) -> None:
    """Deleting pkg/__init__.py can break `import pkg`; the scan keys on the
    package name rather than skipping initializers."""
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text("import pkg\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "pkg" / "__init__.py").unlink()

    refs = find_referencing_paths(tmp_path, ["pkg/__init__.py"])

    assert "consumer.py" in refs["pkg/__init__.py"]


def test_find_referencing_paths_truncation_marker(tmp_path: Path) -> None:
    """More than the cap of referrers are not silently dropped: a marker carrying
    the full count is appended."""
    from software_engineering_team.shared.git_utils import (
        _MAX_REFERRERS_LISTED,
        find_referencing_paths,
    )

    _init_repo(tmp_path)
    (tmp_path / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    total = _MAX_REFERRERS_LISTED + 5
    for i in range(total):
        (tmp_path / f"c{i}.py").write_text("import helper\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "helper.py").unlink()

    refs = find_referencing_paths(tmp_path, ["helper.py"])["helper.py"]

    assert len(refs) == _MAX_REFERRERS_LISTED + 1  # listed + one marker
    assert any("+5 more" in r and f"{total} total" in r for r in refs)


def test_read_paths_at_merge_base_surrogate_safe(tmp_path: Path) -> None:
    """A deleted blob with invalid UTF-8 bytes is returned UTF-8/JSON safe (no
    lone surrogate that would crash a later encode)."""
    from software_engineering_team.shared.git_utils import read_paths_at_merge_base

    _init_repo(tmp_path)
    (tmp_path / "weird.py").write_bytes(b"x = 1  # caf\xe9\xff bytes\n")  # invalid UTF-8, no NUL
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")

    content = read_paths_at_merge_base(tmp_path, "development", ["weird.py"])

    assert "weird.py" in content
    content["weird.py"].encode("utf-8")  # must not raise


def test_whole_repo_review_input_surfaces_omissions(tmp_path: Path) -> None:
    """The whole-repo fallback no longer silently drops sensitive/binary files —
    they are surfaced as omission notes and recorded for the gate."""
    from software_engineering_team.backend_agent.agent import (
        _UNREADABLE_NOTE_PATH,
        _WITHHELD_NOTE_PATH,
        _whole_repo_review_input,
    )

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=v\n", encoding="utf-8")  # sensitive
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")  # binary
    sel = _whole_repo_review_input(tmp_path)

    assert "main.py" in sel.files
    assert ".env" in sel.files[_WITHHELD_NOTE_PATH] and "SECRET=v" not in sel.files[_WITHHELD_NOTE_PATH]
    assert "blob.bin" in sel.files[_UNREADABLE_NOTE_PATH]
    assert ".env" in sel.unexamined_paths() and "blob.bin" in sel.unexamined_paths()


def test_read_repo_files_as_dict_prunes_excluded_dirs_during_walk(tmp_path: Path) -> None:
    """Excluded dirs are pruned during the walk (os.walk), not enumerated then
    discarded — a .git/node_modules subtree is never descended into."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    nm = tmp_path / "node_modules" / "dep"
    nm.mkdir(parents=True)
    (nm / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
    omitted: list[str] = []
    result = read_repo_files_as_dict(tmp_path, omitted=omitted)

    assert "main.py" in result
    assert not any("node_modules" in k for k in result)
    # node_modules entries are pruned (never visited), so not reported as omitted.
    assert not any("node_modules" in o for o in omitted)


def test_select_review_input_deletion_note_lists_dependents(tmp_path: Path) -> None:
    """A deletion whose module is still imported lists the importer inline so the
    reviewer can check the 'nothing depends on it' claim."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text("import helper\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "helper.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "delete helper")

    task = SimpleNamespace(description="x")
    _files, _code, note = _select_review_input(tmp_path, task, written_files=None)

    assert note is not None
    assert "helper.py" in note
    assert "still referenced by: consumer.py" in note


def test_review_input_selection_unpacks_as_triple() -> None:
    """ReviewInputSelection stays unpackable as the legacy (files, code, note)."""
    from software_engineering_team.backend_agent.agent import ReviewInputSelection

    sel = ReviewInputSelection({"a.py": "x"}, None, "note")
    files, code, note = sel
    assert files == {"a.py": "x"} and code is None and note == "note"


def test_has_examinable_content_true_for_real_file() -> None:
    from software_engineering_team.backend_agent.agent import ReviewInputSelection

    sel = ReviewInputSelection({"a.py": "x = 1\n"}, None, None)
    assert sel.has_examinable_content() is True


def test_has_examinable_content_false_for_note_only() -> None:
    from software_engineering_team.backend_agent.agent import (
        _WITHHELD_NOTE_PATH,
        ReviewInputSelection,
    )

    sel = ReviewInputSelection(
        {_WITHHELD_NOTE_PATH: "withheld: app/secrets.py\n"},
        None,
        None,
        synthetic_keys=frozenset({_WITHHELD_NOTE_PATH}),
    )
    assert sel.has_examinable_content() is False  # only an omission note


def test_has_examinable_content_false_for_emptied_only() -> None:
    from software_engineering_team.backend_agent.agent import (
        _EMPTIED_NOTE_PATH,
        ReviewInputSelection,
    )

    # An emptied file (empty content under a real key) plus its note — nothing the
    # reviewer can actually examine.
    sel = ReviewInputSelection(
        {"truncated.py": "   \n", _EMPTIED_NOTE_PATH: "emptied: truncated.py\n"},
        None,
        None,
        synthetic_keys=frozenset({_EMPTIED_NOTE_PATH}),
    )
    assert sel.has_examinable_content() is False


def test_has_examinable_content_nonempty_code_true_empty_repo_false() -> None:
    from software_engineering_team.backend_agent.agent import ReviewInputSelection

    # A non-empty whole-repo code string is examinable; an empty repo (code="")
    # is NOT — nothing was reviewed, so an approval must not be certified.
    assert ReviewInputSelection(None, "### a.py ###\nx", None).has_examinable_content() is True
    assert ReviewInputSelection(None, "", None).has_examinable_content() is False


def test_unexamined_paths_unions_omission_categories() -> None:
    from software_engineering_team.backend_agent.agent import ReviewInputSelection

    sel = ReviewInputSelection(
        {"a.py": "x = 1\n"},
        None,
        None,
        withheld=("secrets.py",),
        unreadable=("img.bin",),
        emptied=("a.py",),
    )
    # Even with an examinable file present, every omitted category is reported so
    # the gate can require manual review for the unexamined parts of the change.
    assert sel.has_examinable_content() is True
    assert sel.unexamined_paths() == ("a.py", "img.bin", "secrets.py")


def test_unexamined_paths_empty_when_all_examined() -> None:
    from software_engineering_team.backend_agent.agent import ReviewInputSelection

    sel = ReviewInputSelection({"a.py": "x = 1\n"}, None, None)
    assert sel.unexamined_paths() == ()


def test_select_review_input_key_to_path_maps_real_paths(tmp_path: Path) -> None:
    """key_to_path lets a finding tagged with a (display-safe) review key be
    translated back to the real on-disk path."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "app" / "main.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change")

    task = SimpleNamespace(description="x")
    sel = _select_review_input(tmp_path, task, written_files=None)

    assert "app/main.py" in sel.files
    assert sel.key_to_path["app/main.py"] == "app/main.py"  # identity for ASCII paths


def test_read_files_as_dict_key_to_path_for_control_char_name(tmp_path: Path) -> None:
    """A control-char filename is sanitized in the key (no prompt injection) while
    key_to_path retains the real path for finding translation."""
    (tmp_path / "a\tb.py").write_text("x = 1\n", encoding="utf-8")
    key_to_path: dict[str, str] = {}

    result = read_files_as_dict(tmp_path, ["a\tb.py"], extensions=None, key_to_path=key_to_path)

    assert "a\\tb.py" in result  # tab escaped in the review key
    assert "a\tb.py" not in result  # raw control char never a key
    assert key_to_path["a\\tb.py"] == "a\tb.py"  # maps back to the real path


# ---------------------------------------------------------------------------
# Gate hardening: baseline-unavailable, unreadable deletions, writer candidates,
# omission preservation, special files, dir symlinks, broader reverse-refs
# ---------------------------------------------------------------------------


def test_select_review_input_baseline_unavailable_forces_manual_review(tmp_path: Path) -> None:
    """When the baseline diff can't be computed, deletions can't be enumerated, so
    the whole-repo fallback records an unexamined sentinel → manual review."""
    from software_engineering_team.backend_agent.agent import (
        _DELETIONS_UNKNOWN_SENTINEL,
        _select_review_input,
    )

    _init_repo(tmp_path)
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    # No `development` branch → merge-base fails → BaselineDiffUnavailable.

    task = SimpleNamespace(description="x")
    sel = _select_review_input(tmp_path, task, written_files={"main.py": "x"})

    assert "main.py" in sel.files  # whole-repo content still reviewed
    assert sel.has_examinable_content() is True
    # ...but the unknown-deletions sentinel forces the approval to manual review.
    assert _DELETIONS_UNKNOWN_SENTINEL in sel.unexamined_paths()


def test_select_review_input_unreadable_deletion_is_unexamined(tmp_path: Path) -> None:
    """A binary file deleted alongside a readable change is recorded unreadable, so
    a mixed change can't be approved with the removed (unexaminable) blob unseen."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "img.bin").write_bytes(b"\x00\x01\x02BIN\x00")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "img.bin").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change + delete binary")

    task = SimpleNamespace(description="x")
    sel = _select_review_input(tmp_path, task, written_files=None)

    assert "a.py" in sel.files  # the readable change is examinable
    assert "img.bin" in sel.unexamined_paths()  # the unreadable deletion blocks approval


def test_select_review_input_writer_candidate_not_omission(tmp_path: Path) -> None:
    """A writer output key that was never materialized on disk (e.g. rejected by
    write validation) is NOT counted as an unexamined omission — otherwise an
    otherwise-clean approved workflow would fail the manual-review gate."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change a")

    task = SimpleNamespace(description="x")
    # 'never_written.py' is a writer candidate not present on disk.
    sel = _select_review_input(
        tmp_path, task, written_files={"a.py": "x", "never_written.py": "rejected"}
    )

    assert "a.py" in sel.files
    assert sel.unexamined_paths() == ()  # the missing candidate is not an omission


def test_whole_repo_review_input_only_sensitive_preserves_evidence(tmp_path: Path) -> None:
    """A repo of only sensitive/binary files returns no readable content, but the
    omission notes/metadata are preserved (not discarded), so the reviewer gets
    the evidence and the gate blocks."""
    from software_engineering_team.backend_agent.agent import (
        _WITHHELD_NOTE_PATH,
        _whole_repo_review_input,
    )

    (tmp_path / ".env").write_text("SECRET=v\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    sel = _whole_repo_review_input(tmp_path)

    assert sel.has_examinable_content() is False  # nothing readable
    assert _WITHHELD_NOTE_PATH in sel.files  # but evidence preserved
    assert ".env" in sel.unexamined_paths()
    assert "blob.bin" in sel.unexamined_paths()


def test_read_files_as_dict_skips_fifo_without_hanging(tmp_path: Path) -> None:
    """A FIFO is reported via omitted and never opened (opening would block)."""
    import os as _os

    fifo = tmp_path / "pipe"
    try:
        _os.mkfifo(fifo)
    except (AttributeError, NotImplementedError, OSError):
        pytest.skip("mkfifo not supported on this platform")
    omitted: list[str] = []
    result = read_files_as_dict(tmp_path, ["pipe"], extensions=None, omitted=omitted)

    assert result == {}  # not read
    assert "pipe" in omitted  # reported, not silently dropped


def test_read_repo_files_as_dict_includes_directory_symlink(tmp_path: Path) -> None:
    """A symlink to a directory is surfaced (by its link target), not silently
    dropped by os.walk's default no-follow behavior."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.py").write_text("x = 1\n", encoding="utf-8")
    try:
        (tmp_path / "linkdir").symlink_to("real", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    result = read_repo_files_as_dict(tmp_path)

    assert "real/f.py" in result
    assert "linkdir" in result and "symlink ->" in result["linkdir"]


def test_run_code_review_rejects_neither_or_both() -> None:
    """The DbC precondition is enforced: exactly one of files/code."""
    from software_engineering_team.backend_agent.agent import BackendExpertAgent

    task = SimpleNamespace(description="t", acceptance_criteria=[])
    with pytest.raises(ValueError):
        BackendExpertAgent._run_code_review(
            code_review_agent=object(), files=None, code=None,
            spec_content="", task=task, architecture=None,
        )
    with pytest.raises(ValueError):
        BackendExpertAgent._run_code_review(
            code_review_agent=object(), files={"a.py": "x"}, code="blob",
            spec_content="", task=task, architecture=None,
        )


def test_find_referencing_paths_relative_import(tmp_path: Path) -> None:
    """A relative `from .helper import x` importer is detected."""
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "pkg" / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    (tmp_path / "pkg" / "user.py").write_text("from .helper import h\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "pkg" / "helper.py").unlink()

    refs = find_referencing_paths(tmp_path, ["pkg/helper.py"])

    assert "pkg/user.py" in refs["pkg/helper.py"]


def test_find_referencing_paths_nested_source_root(tmp_path: Path) -> None:
    """A module under a nested root (backend/app/services.py) matches importers of
    app.services without a hardcoded root list."""
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "backend" / "app").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "services.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "consumer.py").write_text("from app.services import x\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "backend" / "app" / "services.py").unlink()

    refs = find_referencing_paths(tmp_path, ["backend/app/services.py"])

    assert "consumer.py" in refs["backend/app/services.py"]


def test_find_referencing_paths_non_python_basename(tmp_path: Path) -> None:
    """A non-Python deletion still gets a best-effort by-name reverse-ref scan."""
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "widget.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("import { x } from './widget';\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "widget.ts").unlink()

    refs = find_referencing_paths(tmp_path, ["widget.ts"])

    assert "app.ts" in refs["widget.ts"]


def test_list_changed_and_deleted_odd_fields_guarded(tmp_path: Path, monkeypatch) -> None:
    """An odd NUL-field count drops the dangling field instead of mispairing."""
    from software_engineering_team.shared import git_utils

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")

    real_run = git_utils._run_git

    def fake_run(path, cmd, timeout=30):
        if cmd[:3] == ["git", "diff", "--name-status"]:
            return 0, "M\x00a.py\x00D"  # dangling trailing status, no path
        return real_run(path, cmd, timeout)

    monkeypatch.setattr(git_utils, "_run_git", fake_run)
    changed, deleted = git_utils.list_changed_and_deleted(tmp_path, "development")

    assert changed == ["a.py"]
    assert deleted == []  # the dangling 'D' is dropped, not mispaired


# ---------------------------------------------------------------------------
# Gate relaxation + reverse-ref precision + OOM bound + symlink/emptied fixes
# ---------------------------------------------------------------------------


def test_blocking_unexamined_excludes_advisory_includes_suspicious() -> None:
    from software_engineering_team.backend_agent.agent import (
        _DELETIONS_UNKNOWN_SENTINEL,
        ReviewInputSelection,
    )

    # Advisory (NOT blocking): a recognized binary asset and a non-secret template.
    # Blocking: emptied/truncated, the baseline sentinel, a real withheld secret,
    # and an unreadable non-asset (a source file or submodule).
    sel = ReviewInputSelection(
        {"a.py": "x = 1\n"},
        None,
        None,
        withheld=(".env", "app/secrets.py", ".env.example"),
        unreadable=("logo.png", "pkg/mod.py", _DELETIONS_UNKNOWN_SENTINEL),
        emptied=("truncated.py",),
    )
    # Everything omitted is still surfaced for the reviewer.
    assert "logo.png" in sel.unexamined_paths()
    assert ".env.example" in sel.unexamined_paths()

    blocking = sel.blocking_unexamined()
    # Advisory — does NOT block:
    assert "logo.png" not in blocking  # recognized binary asset
    assert ".env.example" not in blocking  # non-secret template
    # Blocking:
    assert "truncated.py" in blocking  # destructive truncation
    assert _DELETIONS_UNKNOWN_SENTINEL in blocking  # unknowable deletions
    assert ".env" in blocking  # real secret
    assert "app/secrets.py" in blocking  # real secret-named source
    assert "pkg/mod.py" in blocking  # unreadable source (non-asset)


def test_select_review_input_binary_asset_does_not_block(tmp_path: Path) -> None:
    """Adding a binary asset alongside code is advisory, not a merge blocker."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "code + binary asset")

    task = SimpleNamespace(description="x")
    sel = _select_review_input(tmp_path, task, written_files=None)

    assert "logo.png" in sel.unexamined_paths()  # surfaced for the reviewer
    assert sel.blocking_unexamined() == ()  # but does not block the merge


def test_select_review_input_emptied_blocks_with_real_path(tmp_path: Path) -> None:
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "important.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "important.py").write_text("   \n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "truncate")

    task = SimpleNamespace(description="x")
    sel = _select_review_input(tmp_path, task, written_files=None)

    assert "important.py" in sel.blocking_unexamined()  # real repo path, blocks


def test_emptied_uses_real_path_not_review_key() -> None:
    """A control-char filename truncated to empty surfaces its REAL path, not the
    escaped review key, in the manual-review set."""
    # Simulate what _select_review_input does: read produces a sanitized key, and
    # emptied maps it back through key_to_path.
    import tempfile

    from software_engineering_team.shared.repo_utils import read_files_as_dict

    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        (p / "a\tb.py").write_text("   \n", encoding="utf-8")
        k2p: dict[str, str] = {}
        files = read_files_as_dict(p, ["a\tb.py"], extensions=None, key_to_path=k2p)
        emptied = [k2p.get(k, k) for k, v in files.items() if not v.strip()]
        assert emptied == ["a\tb.py"]  # real path, not the escaped "a\\tb.py" key


def test_find_referencing_paths_nonpython_precise(tmp_path: Path) -> None:
    """A deleted non-Python file matches path/quote-delimited references, not bare
    occurrences of a common word."""
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "data.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "consumer.js").write_text("import x from './data'\n", encoding="utf-8")
    (tmp_path / "prose.py").write_text("# the data is processed; metadata too\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "data.json").unlink()

    refs = find_referencing_paths(tmp_path, ["data.json"])["data.json"]

    assert "consumer.js" in refs  # './data' is a path-delimited reference
    assert "prose.py" not in refs  # bare word "data" in prose is NOT a false hit


def test_read_paths_at_merge_base_skips_oversized_blob(tmp_path: Path, monkeypatch) -> None:
    from software_engineering_team.shared import git_utils

    _init_repo(tmp_path)
    (tmp_path / "big.py").write_text("x = 1\n" + ("# pad\n" * 100), encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")

    monkeypatch.setattr(git_utils, "_MAX_DELETED_BLOB_BYTES", 10)  # tiny cap
    content = git_utils.read_paths_at_merge_base(tmp_path, "development", ["big.py"])

    assert content == {}  # blob exceeds the cap → not loaded, omitted


def test_read_repo_files_as_dict_excluded_dir_symlink_not_surfaced(tmp_path: Path) -> None:
    """A symlink whose name is an excluded dir (node_modules) is not emitted."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "external").mkdir()
    (tmp_path / "external" / "dep.py").write_text("y = 1\n", encoding="utf-8")
    try:
        (tmp_path / "node_modules").symlink_to("external", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")
    result = read_repo_files_as_dict(tmp_path)

    assert "main.py" in result
    assert not any("node_modules" in k for k in result)  # excluded-name symlink dropped


def test_select_review_input_written_binary_surfaced(tmp_path: Path) -> None:
    """An untracked writer-emitted binary that exists on disk is surfaced as an
    omission note (not a blocker), while a never-written candidate is ignored."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change a")
    # Writer emitted an untracked binary (on disk) + a rejected candidate (absent).
    (tmp_path / "gen.bin").write_bytes(b"\x00\x01binary")

    task = SimpleNamespace(description="x")
    sel = _select_review_input(
        tmp_path, task, written_files={"gen.bin": "x", "never.py": "rejected"}
    )

    assert "gen.bin" in sel.unexamined_paths()  # on-disk binary surfaced
    assert "never.py" not in sel.unexamined_paths()  # absent candidate ignored
    assert sel.blocking_unexamined() == ()  # binary is advisory, not blocking


def test_blocking_real_secret_and_unreadable_source(tmp_path: Path) -> None:
    """A real changed secret and an unreadable source file force manual review;
    a template and a binary asset do not."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=base\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    _git(tmp_path, "branch", "-M", "development")
    _git(tmp_path, "checkout", "-b", "feature/x")
    (tmp_path / "a.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=changed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "change + touch real secret")

    task = SimpleNamespace(description="x")
    sel = _select_review_input(tmp_path, task, written_files=None)

    assert ".env" in sel.blocking_unexamined()  # real secret blocks


def test_read_files_as_dict_omits_oversized_text(tmp_path: Path, monkeypatch) -> None:
    """A text file over the cap is omitted (not loaded whole, not truncated)."""
    from software_engineering_team.shared import repo_utils

    (tmp_path / "big.txt").write_text("a" * 1000, encoding="utf-8")
    monkeypatch.setattr(repo_utils, "_MAX_REVIEW_FILE_BYTES", 100)
    omitted: list[str] = []
    result = repo_utils.read_files_as_dict(tmp_path, ["big.txt"], extensions=None, omitted=omitted)

    assert result == {}
    assert "big.txt" in omitted


def test_read_files_as_dict_binary_without_early_nul(tmp_path: Path) -> None:
    """A binary file whose first chunk has no NUL is still detected (chunked scan)."""
    from software_engineering_team.shared import repo_utils

    # First _READ_CHUNK_BYTES are NUL-free text; a NUL appears later.
    data = b"x" * (repo_utils._READ_CHUNK_BYTES + 10) + b"\x00tail"
    (tmp_path / "weird.bin").write_bytes(data)
    omitted: list[str] = []
    result = repo_utils.read_files_as_dict(tmp_path, ["weird.bin"], extensions=None, omitted=omitted)

    assert result == {}  # detected binary despite the late NUL
    assert "weird.bin" in omitted


def test_read_repo_files_as_dict_skips_worktree_git_file(tmp_path: Path) -> None:
    """A regular file named .git (linked worktree gitlink) is not put in review."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".git").write_text("gitdir: /host/path/.git/worktrees/wt\n", encoding="utf-8")
    result = read_repo_files_as_dict(tmp_path)

    assert "main.py" in result
    assert ".git" not in result  # gitdir metadata not leaked


def test_find_referencing_paths_caps_mass_deletion(tmp_path: Path) -> None:
    """A mass deletion beyond the scan cap skips the per-deletion reverse-ref scan."""
    from software_engineering_team.shared import git_utils

    _init_repo(tmp_path)
    (tmp_path / "consumer.py").write_text("import m0\n", encoding="utf-8")
    for i in range(git_utils._MAX_DELETIONS_SCANNED + 5):
        (tmp_path / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    deleted = [f"m{i}.py" for i in range(git_utils._MAX_DELETIONS_SCANNED + 5)]
    for f in deleted:
        (tmp_path / f).unlink()

    assert git_utils.find_referencing_paths(tmp_path, deleted) == {}  # scan skipped


# ---------------------------------------------------------------------------
# Code-review follow-ups: precise secret-template carve-out, keyword imports,
# .git-only filename exclude
# ---------------------------------------------------------------------------


def test_is_secret_template_path_only_env_family() -> None:
    from software_engineering_team.shared.repo_utils import is_secret_template_path

    # Advisory: placeholder env templates.
    assert is_secret_template_path(".env.example") is True
    assert is_secret_template_path(".env.production.sample") is True
    assert is_secret_template_path("config/.env.template") is True
    # NOT advisory (strong secret signal even with a template suffix):
    assert is_secret_template_path("secrets/config.sample") is False  # secret dir
    assert is_secret_template_path("credentials.template") is False  # credentials stem
    assert is_secret_template_path("secret.dist") is False  # secret stem
    # NOT a template at all:
    assert is_secret_template_path(".env") is False
    assert is_secret_template_path("app/main.py") is False


def test_blocking_secret_template_overlap() -> None:
    """A real secret carrying a template suffix still blocks; only a true env
    template is advisory."""
    from software_engineering_team.backend_agent.agent import ReviewInputSelection

    sel = ReviewInputSelection(
        {"a.py": "x = 1\n"},
        None,
        None,
        withheld=(".env.example", "secrets/api.sample", "credentials.template"),
    )
    blocking = sel.blocking_unexamined()
    assert ".env.example" not in blocking  # genuine env template: advisory
    assert "secrets/api.sample" in blocking  # secret dir: blocks
    assert "credentials.template" in blocking  # credentials stem: blocks


def test_find_referencing_paths_nonpython_bare_keyword_import(tmp_path: Path) -> None:
    """A space-preceded bare `import widget` is recovered by the keyword pattern."""
    from software_engineering_team.shared.git_utils import find_referencing_paths

    _init_repo(tmp_path)
    (tmp_path / "widget.ts").write_text("export const x = 1;\n", encoding="utf-8")
    (tmp_path / "app.ts").write_text("import widget;\n", encoding="utf-8")  # bare, spaced
    (tmp_path / "prose.md").write_text("the widget framework is nice\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    (tmp_path / "widget.ts").unlink()

    refs = find_referencing_paths(tmp_path, ["widget.ts"])["widget.ts"]

    assert "app.ts" in refs  # keyword-anchored bare import matched
    assert "prose.md" not in refs  # bare word in prose still not matched


def test_read_repo_files_as_dict_reviews_file_named_like_excluded_dir(tmp_path: Path) -> None:
    """A regular file literally named like an excluded dir (e.g. `dist`) is still
    reviewed; only a `.git` worktree file is dropped."""
    from software_engineering_team.shared.repo_utils import read_repo_files_as_dict

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "dist").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")  # a script
    (tmp_path / ".git").write_text("gitdir: /host/.git/worktrees/wt\n", encoding="utf-8")
    result = read_repo_files_as_dict(tmp_path)

    assert "main.py" in result
    assert "dist" in result  # real file named like an excluded dir is reviewed
    assert ".git" not in result  # worktree gitlink still dropped
