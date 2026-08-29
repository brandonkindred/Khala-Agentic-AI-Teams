"""Mocked-operation tests for ``shared.git.git_utils``.

Patches ``_run_git`` and ``subprocess.run`` to avoid touching real repos.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def _fake_git_repo(tmp_path: Path) -> Path:
    """Tmp path that ``looks like`` a git repo (.git/ exists) without being one."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_run_git_returns_output(monkeypatch, tmp_path) -> None:
    import subprocess

    from shared.git import git_utils

    class _R:
        returncode = 0
        stdout = "out"
        stderr = "err"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R())
    code, out = git_utils._run_git(tmp_path, ["git", "status"])
    assert code == 0
    assert out == "outerr"


def test_run_git_stderr_suppressed_on_success_but_kept_on_failure(monkeypatch, tmp_path) -> None:
    """merge_stderr=False keeps stdout clean on success, but a non-zero exit still
    surfaces stderr so the failure cause is never lost from the diagnostic."""
    import subprocess

    from shared.git import git_utils

    class _Ok:
        returncode = 0
        stdout = "data"
        stderr = "warning: CRLF\n"

    class _Fail:
        returncode = 128
        stdout = ""
        stderr = "fatal: bad object deadbeef\n"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Ok())
    # Success: stderr warning must not pollute the stdout data channel.
    assert git_utils._run_git(tmp_path, ["git", "show"], merge_stderr=False) == (0, "data")

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _Fail())
    # Failure: stderr is appended even with merge_stderr=False (no data to protect).
    code, out = git_utils._run_git(tmp_path, ["git", "show"], merge_stderr=False)
    assert code == 128
    assert "fatal: bad object deadbeef" in out


def test_run_git_timeout(monkeypatch, tmp_path) -> None:
    import subprocess

    from shared.git import git_utils

    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", boom)
    code, out = git_utils._run_git(tmp_path, ["git", "status"])
    assert code == -1
    assert "timed out" in out


def test_run_git_generic_exception(monkeypatch, tmp_path) -> None:
    import subprocess

    from shared.git import git_utils

    def boom(*a, **kw):
        raise OSError("no git")

    monkeypatch.setattr(subprocess, "run", boom)
    code, out = git_utils._run_git(tmp_path, ["git", "status"])
    assert code == -1
    assert "no git" in out


def test_create_feature_branch_not_a_git_repo(tmp_path: Path) -> None:
    from shared.git.git_utils import create_feature_branch

    ok, msg = create_feature_branch(tmp_path, "main", "t1-x")
    assert ok is False
    assert "Not a git repository" in msg


def test_create_feature_branch_happy_path(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    state = {"i": 0}

    def fake_git(path, cmd, timeout=30):
        state["i"] += 1
        if cmd[:2] == ["git", "status"]:
            return 0, ""
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, name = git_utils.create_feature_branch(_fake_git_repo, "main", "t1-x")
    assert ok is True
    assert name == "feature/t1-x"


def test_create_feature_branch_dirty_tree_commits_first(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    calls = []

    def fake_git(path, cmd, timeout=30):
        calls.append(tuple(cmd))
        if cmd[:2] == ["git", "status"]:
            return 0, "M file.py"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, _ = git_utils.create_feature_branch(_fake_git_repo, "main", "t1-x")
    assert ok is True
    assert any(c[:2] == ("git", "add") for c in calls)


def test_create_feature_branch_already_exists_recreates(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    state = {"i": 0}

    def fake_git(path, cmd, timeout=30):
        state["i"] += 1
        if cmd[:2] == ["git", "status"]:
            return 0, ""
        if cmd[:3] == ["git", "checkout", "-b"]:
            if state["i"] <= 3:
                return 1, "fatal: A branch named 'x' already exists"
            return 0, ""
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, _ = git_utils.create_feature_branch(_fake_git_repo, "main", "feature/x")
    assert ok is True


def test_create_feature_branch_stash_path(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    state = {"i": 0}

    def fake_git(path, cmd, timeout=30):
        state["i"] += 1
        if cmd[:2] == ["git", "status"]:
            return 0, ""  # claim clean tree so we go straight to checkout
        if cmd[:3] == ["git", "checkout", "-b"]:
            if state["i"] == 2:  # first checkout fails with "would be overwritten"
                return 1, "error: Your local changes would be overwritten"
            return 0, ""
        if cmd[:2] == ["git", "stash"]:
            return 0, ""
        return 0, ""

    # No disposable files to clear → falls through to stash
    monkeypatch.setattr(git_utils, "_clear_disposable_files_if_blocking", lambda *_: False)
    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, _ = git_utils.create_feature_branch(_fake_git_repo, "main", "x")
    assert ok is True


def test_create_feature_branch_unknown_failure(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd[:2] == ["git", "status"]:
            return 0, ""
        if cmd[:3] == ["git", "checkout", "-b"]:
            return 1, "fatal: random error"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.create_feature_branch(_fake_git_repo, "main", "x")
    assert ok is False
    assert "random error" in msg


def test_checkout_branch_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import checkout_branch

    ok, msg = checkout_branch(tmp_path, "main")
    assert ok is False
    assert "Not a git repository" in msg


def test_checkout_branch_success(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (0, ""))
    ok, msg = git_utils.checkout_branch(_fake_git_repo, "main")
    assert ok is True


def test_checkout_branch_failure(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (1, "no branch"))
    monkeypatch.setattr(git_utils, "_clear_disposable_files_if_blocking", lambda *_: False)
    ok, msg = git_utils.checkout_branch(_fake_git_repo, "main")
    assert ok is False


def test_write_files_and_commit_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import write_files_and_commit

    ok, msg = write_files_and_commit(tmp_path, {"a.py": "x"}, "msg")
    assert ok is False


def test_write_files_and_commit_no_changes(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd[:2] == ["git", "status"]:
            return 0, ""
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.write_files_and_commit(_fake_git_repo, {"a.py": "x"}, "m")
    assert ok is True
    assert "No changes" in msg


def test_write_files_and_commit_full_flow(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd[:2] == ["git", "status"]:
            return 0, "M a.py"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, _ = git_utils.write_files_and_commit(_fake_git_repo, {"x.py": "code"}, "msg")
    assert ok is True
    assert (_fake_git_repo / "x.py").read_text() == "code"


def test_write_files_and_commit_add_failure(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd[:3] == ["git", "add", "-A"]:
            return 1, "denied"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.write_files_and_commit(_fake_git_repo, {"x.py": "code"}, "msg")
    assert ok is False
    assert "git add failed" in msg


def test_commit_working_tree_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import commit_working_tree

    ok, msg = commit_working_tree(tmp_path, "m")
    assert ok is False


def test_commit_working_tree_no_changes(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.commit_working_tree(_fake_git_repo, "m")
    assert ok is True
    assert "No changes" in msg


def test_commit_working_tree_commit_fails(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd[:2] == ["git", "status"]:
            return 0, "M a"
        if cmd[:2] == ["git", "commit"]:
            return 1, "commit broken"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.commit_working_tree(_fake_git_repo, "m")
    assert ok is False


def test_branch_has_commits_ahead_of_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import branch_has_commits_ahead_of

    assert branch_has_commits_ahead_of(tmp_path, "feature/x", "main") is False


def test_branch_has_commits_ahead_of_true(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (0, "abc commit\n"))
    assert git_utils.branch_has_commits_ahead_of(_fake_git_repo, "feature/x", "main") is True


def test_branch_has_commits_ahead_of_false(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (0, ""))
    assert git_utils.branch_has_commits_ahead_of(_fake_git_repo, "feature/x", "main") is False


def test_merge_branch_success(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (0, ""))
    ok, _ = git_utils.merge_branch(_fake_git_repo, "feature/x", "main")
    assert ok is True


def test_merge_branch_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import merge_branch

    ok, msg = merge_branch(tmp_path, "feature/x", "main")
    assert ok is False


def test_merge_branch_checkout_fail(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd[:2] == ["git", "checkout"]:
            return 1, "checkout err"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.merge_branch(_fake_git_repo, "feature/x", "main")
    assert ok is False


def test_merge_branch_merge_fail(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd[:2] == ["git", "merge"]:
            return 1, "conflict"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.merge_branch(_fake_git_repo, "feature/x", "main")
    assert ok is False


def test_abort_merge_success(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (0, ""))
    ok, _ = git_utils.abort_merge(_fake_git_repo)
    assert ok is True


def test_abort_merge_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import abort_merge

    ok, _ = abort_merge(tmp_path)
    assert ok is False


def test_delete_branch_success(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (0, ""))
    ok, _ = git_utils.delete_branch(_fake_git_repo, "feature/x")
    assert ok is True


def test_delete_branch_failure(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "_run_git", lambda *a, **kw: (1, "no such branch"))
    ok, msg = git_utils.delete_branch(_fake_git_repo, "feature/x")
    assert ok is False


def test_delete_branch_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import delete_branch

    ok, _ = delete_branch(tmp_path, "feature/x")
    assert ok is False


def test_ensure_development_branch_not_a_repo(tmp_path) -> None:
    from shared.git.git_utils import ensure_development_branch

    ok, _ = ensure_development_branch(tmp_path)
    assert ok is False


def test_ensure_development_branch_already_exists(monkeypatch, _fake_git_repo) -> None:
    """Existing development branch returns success after checkout."""
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30, **kwargs):
        if cmd[:2] == ["git", "show-ref"]:
            return 0, ""  # refs/heads/development resolves: branch exists
        if cmd[:3] == ["git", "worktree", "list"]:
            return 0, ""  # not attached in any other worktree
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.ensure_development_branch(_fake_git_repo)
    assert ok is True
    assert "existing" in msg


def test_ensure_development_branch_creates(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30, **kwargs):
        if cmd[:2] == ["git", "show-ref"]:
            return 1, ""  # refs/heads/development does not resolve yet
        if cmd[:3] == ["git", "branch", "-a"]:
            return 0, "* main\n"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    created, msg = git_utils.ensure_development_branch(_fake_git_repo)
    assert created is True
    assert "Created" in msg


def test_ensure_development_branch_no_base(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30, **kwargs):
        if cmd[:2] == ["git", "show-ref"]:
            return 1, ""  # refs/heads/development does not resolve yet
        if cmd[:3] == ["git", "branch", "-a"]:
            return 0, ""
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    created, msg = git_utils.ensure_development_branch(_fake_git_repo)
    assert created is False
    assert "Neither" in msg


def test_initialize_new_repo_already_a_repo(monkeypatch, _fake_git_repo) -> None:
    from shared.git import git_utils

    monkeypatch.setattr(git_utils, "ensure_development_branch", lambda p: (True, "Created"))
    ok, msg = git_utils.initialize_new_repo(_fake_git_repo)
    assert ok is True
    assert "Already a git repo" in msg


def test_initialize_new_repo_fresh(monkeypatch, tmp_path) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd == ["git", "branch", "--show-current"]:
            return 0, "main\n"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.initialize_new_repo(tmp_path)
    assert ok is True
    # The function writes README.md, CONTRIBUTORS.md, docs/ etc.
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / "CONTRIBUTORS.md").exists()


def test_initialize_new_repo_init_fails(monkeypatch, tmp_path) -> None:
    from shared.git import git_utils

    def fake_git(path, cmd, timeout=30):
        if cmd == ["git", "init"]:
            return 1, "init err"
        return 0, ""

    monkeypatch.setattr(git_utils, "_run_git", fake_git)
    ok, msg = git_utils.initialize_new_repo(tmp_path)
    assert ok is False
