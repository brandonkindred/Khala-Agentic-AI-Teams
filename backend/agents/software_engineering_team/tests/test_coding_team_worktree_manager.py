"""Tests for coding_team.worktree_manager.WorktreeManager.

Real git subprocesses against a tmp_path repo — worktree isolation is the
entire point of this class, so these prove real git semantics hold rather
than asserting a mocked subprocess call was built.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from shared.git.git_utils import DEVELOPMENT_BRANCH, initialize_new_repo, remove_worktree
from software_engineering_team.agent_status import (
    CODING_TEAM_WORKERS_PER_STACK_ENV,
    derive_stack_roster,
)
from software_engineering_team.worktree_manager import (
    WorktreeManager,
    WorktreePrepareError,
)


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
    import software_engineering_team.worktree_manager as wm_mod

    monkeypatch.setattr(wm_mod, "add_worktree", lambda *a, **k: (False, "boom"))
    manager = WorktreeManager(repo, ["backend_v2"])

    with pytest.raises(WorktreePrepareError, match="boom"):
        manager.prepare()


@pytest.mark.parametrize(
    "unsafe_agent_id",
    [
        "../../evil",
        "..",
        "a/../../evil",
        "",
    ],
)
def test_prepare_rejects_unsafe_agent_id_as_path_component(
    repo: Path, unsafe_agent_id: str
) -> None:
    """An agent id that would escape the worktree root via traversal, or is empty, is rejected
    before any worktree is created — agent ids ultimately trace back to Tech-Lead-generated or
    persisted stack names, not a fully trusted source."""
    manager = WorktreeManager(repo, [unsafe_agent_id])

    with pytest.raises(WorktreePrepareError, match="Unsafe agent id"):
        manager.prepare()

    # Nothing was created outside the intended worktree root.
    assert not (repo.parent / "evil").exists()
    assert not (repo.parent.parent / "evil").exists()


def test_prepare_accepts_agent_id_that_normalizes_safely_under_root(repo: Path) -> None:
    """A leading-slash agent id is neutralized to a safe relative path under the worktree root
    (mirrors resolve_safe_repo_path's existing contract for repo-relative writes) rather than
    being treated as an absolute filesystem path."""
    manager = WorktreeManager(repo, ["/backend_v2"])

    manager.prepare()

    wt_path = manager.path_for("/backend_v2")
    assert wt_path.exists()
    assert wt_path.parent == repo.parent / f".{repo.name}.worktrees"


def test_prepare_records_partial_worktrees_for_cleanup_on_mid_loop_failure(
    repo: Path, monkeypatch
) -> None:
    """If the first agent's worktree is created successfully but a later agent's fails, the
    first one must still be tracked so cleanup() can remove it — not silently left behind as an
    orphaned worktree + admin-area entry."""
    import software_engineering_team.worktree_manager as wm_mod

    real_add_worktree = wm_mod.add_worktree
    calls = {"n": 0}

    def _fail_on_second(repo_path, worktree_path, ref=None):
        calls["n"] += 1
        if calls["n"] == 2:
            return False, "boom on second agent"
        return real_add_worktree(repo_path, worktree_path, ref=ref)

    monkeypatch.setattr(wm_mod, "add_worktree", _fail_on_second)
    manager = WorktreeManager(repo, ["backend_v2", "frontend_v2"])

    with pytest.raises(WorktreePrepareError, match="boom on second agent"):
        manager.prepare()

    first_agent_path = repo.parent / f".{repo.name}.worktrees" / "backend_v2"
    assert first_agent_path.exists()  # the first worktree really was created
    assert manager._paths == {"backend_v2": first_agent_path}  # and IS tracked

    manager.cleanup()

    assert not first_agent_path.exists()  # cleanup actually found and removed it


def test_prepare_raises_when_root_repo_initialization_fails(tmp_path: Path, monkeypatch) -> None:
    import software_engineering_team.worktree_manager as wm_mod

    fresh_path = tmp_path / "uninitializable"
    fresh_path.mkdir()
    monkeypatch.setattr(wm_mod, "initialize_new_repo", lambda *a, **k: (False, "disk full"))

    manager = WorktreeManager(fresh_path, ["backend_v2"])
    with pytest.raises(WorktreePrepareError, match="disk full"):
        manager.prepare()


def test_prepare_raises_when_development_branch_cannot_be_ensured(repo: Path, monkeypatch) -> None:
    import software_engineering_team.worktree_manager as wm_mod

    monkeypatch.setattr(wm_mod, "development_branch_exists", lambda *a, **k: False)
    monkeypatch.setattr(wm_mod, "ensure_development_branch", lambda *a, **k: (False, "locked"))

    manager = WorktreeManager(repo, ["backend_v2"])
    with pytest.raises(WorktreePrepareError, match="locked"):
        manager.prepare()


def test_cleanup_logs_but_does_not_raise_when_remove_fails(repo: Path, monkeypatch) -> None:
    import software_engineering_team.worktree_manager as wm_mod

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


# --- N same-stack workers (widened roster: e.g. "backend_v2-1", "backend_v2-2") ---


def test_prepare_creates_isolated_worktrees_for_same_stack_agent_ids(repo: Path) -> None:
    """A widened same-stack roster (three backend_v2 workers) gets three distinct,
    mutually-isolated worktrees — allocation/isolation was never actually gated on
    stack kind, only on agent_id uniqueness."""
    same_stack_ids = ["backend_v2-1", "backend_v2-2", "backend_v2-3"]
    manager = WorktreeManager(repo, same_stack_ids)
    manager.prepare()

    paths = {aid: manager.path_for(aid) for aid in same_stack_ids}
    assert len(set(paths.values())) == 3  # all distinct
    for aid, path in paths.items():
        assert path.exists() and (path / ".git").exists()
        (path / f"{aid}-only.txt").write_text(aid, encoding="utf-8")

    for aid, path in paths.items():
        for other_aid, other_path in paths.items():
            if other_aid == aid:
                continue
            assert not (other_path / f"{aid}-only.txt").exists()
        assert not (repo / f"{aid}-only.txt").exists()


def test_node_modules_symlink_independent_across_same_stack_worktrees(repo: Path) -> None:
    """2+ concurrent same-stack frontend worktrees each get their own independent
    symlink into the one shared, repo-level node_modules — genuinely shared content
    (not copied), and removing one worktree never disturbs a sibling's symlink or the
    shared directory itself."""
    (repo / "package.json").write_text("{}", encoding="utf-8")
    node_modules = repo / "node_modules"
    node_modules.mkdir()
    marker = node_modules / "shared-pkg" / "index.js"
    marker.parent.mkdir()
    marker.write_text("original", encoding="utf-8")

    same_stack_ids = ["frontend_v2-1", "frontend_v2-2"]
    manager = WorktreeManager(repo, same_stack_ids)
    manager.prepare()

    links = {aid: manager.path_for(aid) / "node_modules" for aid in same_stack_ids}
    for link in links.values():
        assert link.is_symlink()

    # Genuinely shared, not per-worktree copies: a write through one worktree's
    # symlink is visible through the other's and at the repo-root source.
    (links["frontend_v2-1"] / "shared-pkg" / "index.js").write_text("changed", encoding="utf-8")
    assert (links["frontend_v2-2"] / "shared-pkg" / "index.js").read_text(
        encoding="utf-8"
    ) == "changed"
    assert marker.read_text(encoding="utf-8") == "changed"

    # Removing one same-stack worktree must not affect its sibling's symlink or the
    # shared node_modules directory both point at.
    ok, msg = remove_worktree(repo, manager.path_for("frontend_v2-1"), force=True)
    assert ok, msg

    assert links["frontend_v2-2"].is_symlink()
    assert (links["frontend_v2-2"] / "shared-pkg" / "index.js").read_text(
        encoding="utf-8"
    ) == "changed"
    assert marker.read_text(encoding="utf-8") == "changed"


def test_cleanup_prunes_only_finished_worker_not_same_stack_sibling(repo: Path) -> None:
    """A WorktreeManager scoped to one same-stack worker's cleanup() must never remove
    a sibling same-stack worker's worktree — pruning is per-agent, never cross-worker,
    even when both share a stack kind and are managed independently."""
    finished = WorktreeManager(repo, ["backend_v2-1"])
    finished.prepare()
    finished_path = finished.path_for("backend_v2-1")

    still_running = WorktreeManager(repo, ["backend_v2-2"])
    still_running.prepare()
    running_path = still_running.path_for("backend_v2-2")

    finished.cleanup()

    assert not finished_path.exists()
    assert running_path.exists() and (running_path / ".git").exists()


def test_prepare_and_use_two_same_stack_worktrees_from_concurrent_threads(repo: Path) -> None:
    """After prepare() completes, two same-stack workers can concurrently use their
    already-prepared worktrees (path_for is documented as a pure, lock-free lookup)
    without any cross-worker file leakage."""
    same_stack_ids = ["backend_v2-1", "backend_v2-2"]
    manager = WorktreeManager(repo, same_stack_ids)
    manager.prepare()

    errors: list[BaseException] = []

    def _worker(agent_id: str) -> None:
        try:
            path = manager.path_for(agent_id)
            for i in range(20):
                (path / f"{agent_id}-{i}.txt").write_text(agent_id, encoding="utf-8")
        except BaseException as exc:  # noqa: BLE001 - surfaced via `errors` below
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(aid,)) for aid in same_stack_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    path1, path2 = manager.path_for("backend_v2-1"), manager.path_for("backend_v2-2")
    for i in range(20):
        assert (path1 / f"backend_v2-1-{i}.txt").exists()
        assert not (path1 / f"backend_v2-2-{i}.txt").exists()
        assert (path2 / f"backend_v2-2-{i}.txt").exists()
        assert not (path2 / f"backend_v2-1-{i}.txt").exists()


def test_derive_stack_roster_ids_produce_isolated_worktrees(repo: Path, monkeypatch) -> None:
    """The real production naming scheme (derive_stack_roster under
    CODING_TEAM_WORKERS_PER_STACK=N) feeds straight into WorktreeManager without
    drift: both widened-roster agent_ids are safe path components that resolve to
    distinct, isolated worktrees."""
    monkeypatch.setenv(CODING_TEAM_WORKERS_PER_STACK_ENV, "2")
    roster = derive_stack_roster([{"name": "backend_v2"}])
    agent_ids = [entry.agent_id for entry in roster]
    assert agent_ids == ["backend_v2-1", "backend_v2-2"]

    manager = WorktreeManager(repo, agent_ids)
    manager.prepare()

    path1, path2 = manager.path_for("backend_v2-1"), manager.path_for("backend_v2-2")
    assert path1 != path2
    assert path1.exists() and (path1 / ".git").exists()
    assert path2.exists() and (path2 / ".git").exists()
