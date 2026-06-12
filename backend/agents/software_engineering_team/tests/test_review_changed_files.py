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

    def fake_run_git(path, cmd, timeout=30):
        if cmd[:2] == ["git", "merge-base"]:
            return 0, "1111111111111111111111111111111111111111\n2222222222222222222222222222222222222222\n"
        return 0, ""

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
    """A changed secret (.env, key) is not forwarded to the review model."""
    from software_engineering_team.backend_agent.agent import _select_review_input

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
    assert ".env" not in files  # secret excluded from the review payload


def test_select_review_input_excludes_deleted_secret_from_note(tmp_path: Path) -> None:
    """A deleted secret's path is not surfaced in the deletion note."""
    from software_engineering_team.backend_agent.agent import _select_review_input

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
    _files, _code, note = _select_review_input(tmp_path, task, written_files=None)

    assert note is not None
    assert "gone.py" in note  # ordinary deletion surfaced
    assert ".env" not in note  # secret path name not surfaced


def test_select_review_input_fails_closed_to_whole_repo(tmp_path: Path) -> None:
    """When the baseline diff can't be computed, review the whole repo (closed)."""
    from software_engineering_team.backend_agent.agent import _select_review_input

    _init_repo(tmp_path)
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")
    # No `development` branch exists → merge-base fails → fail closed.

    task = SimpleNamespace(description="x")
    # written_files present, but the partial set must not be used on baseline failure.
    files, code, _note = _select_review_input(tmp_path, task, written_files={"main.py": "x"})

    assert code is None
    assert "main.py" in files  # complete whole-repo review (as a files dict)


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
