"""Tests for coding_team.worktree_manager.WorktreeManager.

Real git subprocesses against a tmp_path repo — worktree isolation is the
entire point of this class, so these prove real git semantics hold rather
than asserting a mocked subprocess call was built.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coding_team.worktree_manager import WorktreeManager, WorktreePrepareError
from shared_git.git_utils import DEVELOPMENT_BRANCH, initialize_new_repo


def _init_repo(path: Path) -> None:
    ok, msg = initialize_new_repo(path)
    assert ok, msg


def _current_branch(path: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=path, capture_output=True, text=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    _init_repo(path)
    return path


def test_prepare_creates_distinct_worktrees_for_each_agent(repo: Path) -> None:
    manager = WorktreeManager(repo, ["frontend_v2", "backend_v2"])
    manager.prepare()

    fe_path = manager.path_for("frontend_v2")
    be_path = manager.path_for("backend_v2")
    assert fe_path != be_path
    assert fe_path.exists() and (fe_path / ".git").exists()
    assert be_path.exists() and (be_path / ".git").exists()
    # Sibling to repo, never nested inside it.
    assert repo not in fe_path.parents or fe_path.parent != repo
    assert fe_path.parent == repo.parent / f".{repo.name}.worktrees"


def test_prepare_worktrees_are_isolated_working_directories(repo: Path) -> None:
    manager = WorktreeManager(repo, ["frontend_v2", "backend_v2"])
    manager.prepare()

    fe_path = manager.path_for("frontend_v2")
    be_path = manager.path_for("backend_v2")
    (fe_path / "frontend-only.txt").write_text("fe", encoding="utf-8")
    (be_path / "backend-only.txt").write_text("be", encoding="utf-8")

    assert not (be_path / "frontend-only.txt").exists()
    assert not (fe_path / "backend-only.txt").exists()
    assert not (repo / "frontend-only.txt").exists()
    assert not (repo / "backend-only.txt").exists()


def test_prepare_initializes_a_brand_new_repo_path(tmp_path: Path) -> None:
    """A coding-team job whose checkout hasn't been git-initialized yet (the
    first-ever task previously triggered this lazily from inside the worker)
    gets initialized here, up front, before any worktree is added."""
    fresh_path = tmp_path / "fresh-job-repo"
    fresh_path.mkdir()
    manager = WorktreeManager(fresh_path, ["backend_v2"])

    manager.prepare()

    assert (fresh_path / ".git").exists()
    assert _current_branch(fresh_path) == DEVELOPMENT_BRANCH
    assert manager.path_for("backend_v2").exists()


def test_prepare_is_idempotent(repo: Path) -> None:
    manager = WorktreeManager(repo, ["backend_v2"])
    manager.prepare()
    first_path = manager.path_for("backend_v2")

    manager.prepare()  # second call is a no-op

    assert manager.path_for("backend_v2") == first_path


def test_prepare_self_heals_worktree_left_by_a_crashed_run(repo: Path) -> None:
    """A worktree directory (and admin-area registration) left behind by a
    killed prior job attempt is cleared and recreated fresh."""
    manager1 = WorktreeManager(repo, ["backend_v2"])
    manager1.prepare()
    stale_path = manager1.path_for("backend_v2")
    (stale_path / "leftover-in-progress-work.txt").write_text("x", encoding="utf-8")
    # No cleanup() call — simulates the process dying mid-job.

    manager2 = WorktreeManager(repo, ["backend_v2"])
    manager2.prepare()

    fresh_path = manager2.path_for("backend_v2")
    assert fresh_path == stale_path
    assert not (fresh_path / "leftover-in-progress-work.txt").exists()


def test_cleanup_removes_worktrees_and_leaves_repo_untouched(repo: Path) -> None:
    manager = WorktreeManager(repo, ["frontend_v2", "backend_v2"])
    manager.prepare()
    fe_path = manager.path_for("frontend_v2")
    be_path = manager.path_for("backend_v2")
    assert _current_branch(repo) == DEVELOPMENT_BRANCH

    manager.cleanup()

    assert not fe_path.exists()
    assert not be_path.exists()
    assert repo.exists()
    assert _current_branch(repo) == DEVELOPMENT_BRANCH  # untouched


def test_cleanup_is_idempotent_and_safe_before_prepare(repo: Path) -> None:
    manager = WorktreeManager(repo, ["backend_v2"])
    manager.cleanup()  # never prepared — must not raise

    manager.prepare()
    manager.cleanup()
    manager.cleanup()  # second cleanup — must not raise


def test_path_for_fails_closed_before_prepare(repo: Path) -> None:
    manager = WorktreeManager(repo, ["backend_v2"])
    with pytest.raises(WorktreePrepareError):
        manager.path_for("backend_v2")


def test_path_for_fails_closed_for_unknown_agent_id(repo: Path) -> None:
    manager = WorktreeManager(repo, ["backend_v2"])
    manager.prepare()
    with pytest.raises(WorktreePrepareError):
        manager.path_for("frontend_v2")


def test_prepare_raises_worktree_prepare_error_on_git_failure(repo: Path, monkeypatch) -> None:
    import coding_team.worktree_manager as wm_mod

    monkeypatch.setattr(wm_mod, "add_worktree", lambda *a, **k: (False, "boom"))
    manager = WorktreeManager(repo, ["backend_v2"])

    with pytest.raises(WorktreePrepareError, match="boom"):
        manager.prepare()


def test_prepare_raises_when_root_repo_initialization_fails(tmp_path: Path, monkeypatch) -> None:
    import coding_team.worktree_manager as wm_mod

    fresh_path = tmp_path / "uninitializable"
    fresh_path.mkdir()
    monkeypatch.setattr(wm_mod, "initialize_new_repo", lambda *a, **k: (False, "disk full"))

    manager = WorktreeManager(fresh_path, ["backend_v2"])
    with pytest.raises(WorktreePrepareError, match="disk full"):
        manager.prepare()


def test_prepare_raises_when_development_branch_cannot_be_ensured(repo: Path, monkeypatch) -> None:
    import coding_team.worktree_manager as wm_mod

    monkeypatch.setattr(wm_mod, "development_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(wm_mod, "ensure_development_branch", lambda *a, **k: (False, "locked"))

    manager = WorktreeManager(repo, ["backend_v2"])
    with pytest.raises(WorktreePrepareError, match="locked"):
        manager.prepare()


def test_cleanup_logs_but_does_not_raise_when_remove_fails(repo: Path, monkeypatch) -> None:
    import coding_team.worktree_manager as wm_mod

    manager = WorktreeManager(repo, ["backend_v2"])
    manager.prepare()
    monkeypatch.setattr(wm_mod, "remove_worktree", lambda *a, **k: (False, "still in use"))

    manager.cleanup()  # must not raise

    assert manager._paths == {}


def test_duplicate_agent_ids_produce_one_worktree(repo: Path) -> None:
    manager = WorktreeManager(repo, ["backend_v2", "backend_v2"])
    manager.prepare()
    assert manager.path_for("backend_v2").exists()
    manager.cleanup()  # must not double-remove / error


def test_node_modules_symlink_shared_from_repo_root(repo: Path) -> None:
    (repo / "package.json").write_text("{}", encoding="utf-8")
    node_modules = repo / "node_modules"
    node_modules.mkdir()
    marker = node_modules / "some-pkg"
    marker.mkdir()
    (marker / "index.js").write_text("module.exports = {};", encoding="utf-8")

    manager = WorktreeManager(repo, ["frontend_v2"])
    manager.prepare()
    wt_path = manager.path_for("frontend_v2")

    linked = wt_path / "node_modules"
    assert linked.is_symlink()
    assert (linked / "some-pkg" / "index.js").read_text(encoding="utf-8") == (
        "module.exports = {};"
    )
    # Content is genuinely shared, not copied: a change from the worktree side
    # is visible from the repo side.
    (linked / "some-pkg" / "index.js").write_text("changed", encoding="utf-8")
    assert (marker / "index.js").read_text(encoding="utf-8") == "changed"


def test_node_modules_symlink_shared_from_frontend_subdir(repo: Path) -> None:
    frontend_dir = repo / "frontend"
    frontend_dir.mkdir()
    (frontend_dir / "package.json").write_text("{}", encoding="utf-8")
    node_modules = frontend_dir / "node_modules"
    node_modules.mkdir()
    (node_modules / "marker.txt").write_text("m", encoding="utf-8")

    manager = WorktreeManager(repo, ["frontend_v2"])
    manager.prepare()
    wt_path = manager.path_for("frontend_v2")

    linked = wt_path / "frontend" / "node_modules"
    assert linked.is_symlink()
    assert (linked / "marker.txt").read_text(encoding="utf-8") == "m"


def test_no_node_modules_symlink_when_none_present(repo: Path) -> None:
    manager = WorktreeManager(repo, ["backend_v2"])
    manager.prepare()
    wt_path = manager.path_for("backend_v2")
    assert not (wt_path / "node_modules").exists()


def test_cleanup_never_follows_symlink_into_repo_node_modules(repo: Path) -> None:
    """Removing a worktree must not touch the shared node_modules it symlinks to."""
    (repo / "package.json").write_text("{}", encoding="utf-8")
    node_modules = repo / "node_modules"
    node_modules.mkdir()
    (node_modules / "keep.txt").write_text("keep", encoding="utf-8")

    manager = WorktreeManager(repo, ["frontend_v2"])
    manager.prepare()
    manager.cleanup()

    assert (node_modules / "keep.txt").exists()


def test_symlink_node_modules_is_idempotent_when_already_linked(repo: Path) -> None:
    """Re-running the symlink step against an already-linked worktree is a no-op, not an error."""
    (repo / "package.json").write_text("{}", encoding="utf-8")
    node_modules = repo / "node_modules"
    node_modules.mkdir()

    manager = WorktreeManager(repo, ["backend_v2"])
    manager.prepare()
    wt_path = manager.path_for("backend_v2")
    assert (wt_path / "node_modules").is_symlink()

    manager._symlink_node_modules(wt_path, node_modules)  # second call: must not raise

    assert (wt_path / "node_modules").is_symlink()


def test_node_modules_symlink_failure_does_not_fail_prepare(repo: Path, monkeypatch) -> None:
    """A symlink failure (e.g. no OS support) is logged, not raised — the worktree is still
    usable without it (a backend task, or a frontend task that tolerates a slow reinstall)."""
    (repo / "package.json").write_text("{}", encoding="utf-8")
    node_modules = repo / "node_modules"
    node_modules.mkdir()

    def _boom_symlink(self, *a, **k):
        raise OSError("no symlink support")

    monkeypatch.setattr(Path, "symlink_to", _boom_symlink)

    manager = WorktreeManager(repo, ["frontend_v2"])
    manager.prepare()  # must not raise despite the symlink failure

    wt_path = manager.path_for("frontend_v2")
    assert not (wt_path / "node_modules").exists()
