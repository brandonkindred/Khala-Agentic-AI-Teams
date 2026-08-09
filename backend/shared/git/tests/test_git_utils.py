"""Tests for the git-worktree primitives added to :mod:`shared.git.git_utils`.

These exercise real ``git`` subprocesses against a ``tmp_path`` repository
rather than mocking git calls: worktree isolation is the entire point of the
feature these helpers support (issue: parallelize coding_team implementation
workers via per-worker git worktrees), so the tests need to prove real git
checkout-exclusivity semantics hold, not just that the expected subprocess
command string was built.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared.git.git_utils import (
    DEVELOPMENT_BRANCH,
    add_worktree,
    checkout_branch,
    clean_untracked_files,
    create_feature_branch,
    development_branch_exists,
    ensure_development_branch,
    initialize_new_repo,
    prune_worktrees,
    remove_worktree,
    reset_hard_to,
)


def _init_repo(path: Path) -> None:
    ok, msg = initialize_new_repo(path)
    assert ok, msg


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    _init_repo(path)
    return path


def test_development_branch_exists_true_after_init(repo: Path) -> None:
    assert development_branch_exists(repo) is True


def test_development_branch_exists_false_before_creation(tmp_path: Path) -> None:
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    subprocess.run(["git", "init"], cwd=fresh, capture_output=True, check=True)
    assert development_branch_exists(fresh) is False


def test_development_branch_exists_false_for_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert development_branch_exists(not_a_repo) is False


def test_add_worktree_creates_linked_worktree_detached_at_ref(repo: Path) -> None:
    wt_path = repo.parent / "wt1"
    ok, msg = add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    assert ok, msg
    assert wt_path.exists()
    assert (wt_path / ".git").exists()
    # Detached HEAD, not attached to development: `git symbolic-ref` fails.
    result = subprocess.run(
        ["git", "symbolic-ref", "-q", "HEAD"], cwd=wt_path, capture_output=True, text=True
    )
    assert result.returncode != 0


def test_add_worktree_fails_for_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    ok, msg = add_worktree(not_a_repo, tmp_path / "wt")
    assert not ok
    assert "Not a git repository" in msg


def test_add_worktree_self_heals_stale_directory(repo: Path) -> None:
    """A leftover directory from a crashed prior run (unregistered with git)
    is cleared and the worktree is created fresh on retry."""
    wt_path = repo.parent / "wt-stale"
    wt_path.mkdir(parents=True)
    (wt_path / "leftover.txt").write_text("stale", encoding="utf-8")

    ok, msg = add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    assert ok, msg
    assert (wt_path / ".git").exists()
    assert not (wt_path / "leftover.txt").exists()


def test_create_feature_branch_succeeds_in_worktree_while_development_checked_out_elsewhere(
    repo: Path,
) -> None:
    """The crux claim this whole design rests on: creating a feature branch in
    a linked worktree (via ``git checkout -b <new> development``) succeeds even
    while ``development`` remains attached (checked out) at the main repo path.
    """
    # repo is already on `development` (initialize_new_repo leaves it checked out).
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert result.stdout.strip() == DEVELOPMENT_BRANCH

    wt_path = repo.parent / "wt-feature"
    ok, msg = add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    assert ok, msg

    # development is STILL checked out at repo while we branch in the worktree.
    ok, branch_name = create_feature_branch(wt_path, DEVELOPMENT_BRANCH, "t1-my-task")
    assert ok, branch_name
    assert branch_name == "feature/t1-my-task"

    # repo's own checkout is untouched.
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert result.stdout.strip() == DEVELOPMENT_BRANCH


def test_ensure_development_branch_succeeds_without_attaching_in_a_second_worktree(
    repo: Path,
) -> None:
    """``ensure_development_branch`` must not fail (or hang trying to attach
    development) when called from a linked worktree while ``development``
    stays checked out at the main repo path — the real-v2-team-lead setup
    phase (``software_engineering_team/shared/phases/setup.py``) calls this
    unconditionally on a worker's worktree that already has a feature branch
    checked out. ``git branch -a`` marks development ``+`` there (checked out
    in *another* worktree), which naive ``.lstrip("* ")`` parsing collapses
    into "not the current branch", previously causing a doomed
    ``checkout -b development`` that collided with the existing ref. This
    must instead report success and leave the worktree's own checkout alone.
    """
    wt_path = repo.parent / "wt-conflict"
    ok, msg = add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    assert ok, msg
    ok, branch = create_feature_branch(wt_path, DEVELOPMENT_BRANCH, "t4-setup")
    assert ok, branch

    ok, msg = ensure_development_branch(wt_path)

    assert ok, msg
    # The worktree's own checkout (the feature branch) is untouched.
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=wt_path, capture_output=True, text=True
    )
    assert result.stdout.strip() == branch
    # development is still attached at the main repo path, unaffected.
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert result.stdout.strip() == DEVELOPMENT_BRANCH


def test_ensure_development_branch_checks_out_development_when_not_attached_elsewhere(
    repo: Path,
) -> None:
    """The normal (single-worktree) case is unchanged: switching off development
    onto a feature branch and back is a plain, successful checkout — no other
    worktree has development attached, so the new elsewhere-check is a no-op."""
    ok, branch = create_feature_branch(repo, DEVELOPMENT_BRANCH, "t5-normal")
    assert ok, branch

    ok, msg = ensure_development_branch(repo)

    assert ok, msg
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert result.stdout.strip() == DEVELOPMENT_BRANCH


def test_ensure_development_branch_fails_closed_when_worktree_list_query_fails(
    repo: Path, monkeypatch
) -> None:
    """A ``git worktree list`` failure must not be silently treated as "not
    attached elsewhere" and skip the checkout — it falls back to attempting
    the normal checkout, surfacing any real conflict through that call's own
    error instead of masking the query failure."""
    import shared.git.git_utils as git_utils_mod

    ok, branch = create_feature_branch(repo, DEVELOPMENT_BRANCH, "t6-query-fails")
    assert ok, branch

    real_run_git = git_utils_mod._run_git

    def _flaky_run_git(path, cmd, *a, **k):
        if cmd[:3] == ["git", "worktree", "list"]:
            return 1, "boom"
        return real_run_git(path, cmd, *a, **k)

    monkeypatch.setattr(git_utils_mod, "_run_git", _flaky_run_git)

    ok, msg = ensure_development_branch(repo)

    assert ok, msg
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert result.stdout.strip() == DEVELOPMENT_BRANCH


def test_checkout_branch_is_idempotent_on_worktree_already_on_that_branch(repo: Path) -> None:
    wt_path = repo.parent / "wt-idempotent"
    add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    ok, _ = create_feature_branch(wt_path, DEVELOPMENT_BRANCH, "t2")
    assert ok
    # Re-checking-out the branch the worktree is already on is a harmless no-op.
    ok, msg = checkout_branch(wt_path, "feature/t2")
    assert ok, msg


def test_remove_worktree_deletes_directory_and_deregisters(repo: Path) -> None:
    wt_path = repo.parent / "wt-remove"
    add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    assert wt_path.exists()

    ok, msg = remove_worktree(repo, wt_path)
    assert ok, msg
    assert not wt_path.exists()

    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    assert str(wt_path) not in result.stdout


def test_remove_worktree_is_idempotent_when_already_gone(repo: Path) -> None:
    wt_path = repo.parent / "wt-gone"
    ok, msg = remove_worktree(repo, wt_path)
    assert ok, msg


def test_remove_worktree_force_removes_dirty_worktree(repo: Path) -> None:
    wt_path = repo.parent / "wt-dirty"
    add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    (wt_path / "scratch.txt").write_text("uncommitted", encoding="utf-8")

    ok, msg = remove_worktree(repo, wt_path, force=True)
    assert ok, msg
    assert not wt_path.exists()


def test_prune_worktrees_clears_stale_registration_after_manual_rmtree(repo: Path) -> None:
    import shutil

    wt_path = repo.parent / "wt-manual-remove"
    add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    shutil.rmtree(wt_path)  # simulate an external deletion, bypassing git

    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    assert str(wt_path) in result.stdout  # still registered (prunable) before prune

    ok, msg = prune_worktrees(repo)
    assert ok, msg

    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, capture_output=True, text=True
    )
    assert str(wt_path) not in result.stdout


def test_prune_worktrees_noop_for_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    ok, msg = prune_worktrees(not_a_repo)
    assert ok, msg


def test_add_worktree_fails_after_retry_when_ref_does_not_exist(repo: Path) -> None:
    """A genuinely bad ref fails both the first attempt and the self-heal retry."""
    wt_path = repo.parent / "wt-bad-ref"
    ok, msg = add_worktree(repo, wt_path, ref="no-such-branch")
    assert not ok
    assert "Failed to add worktree" in msg


def test_remove_worktree_fails_for_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    ok, msg = remove_worktree(not_a_repo, tmp_path / "wt")
    assert not ok
    assert "Not a git repository" in msg


def test_remove_worktree_falls_back_to_filesystem_when_git_does_not_recognize_it(
    repo: Path,
) -> None:
    """A directory git doesn't track as one of its worktrees still gets cleaned up via the
    filesystem-rmtree fallback (git worktree remove fails, but removal still succeeds)."""
    untracked_dir = repo.parent / "not-a-real-worktree"
    untracked_dir.mkdir()
    (untracked_dir / "file.txt").write_text("x", encoding="utf-8")

    ok, msg = remove_worktree(repo, untracked_dir)

    assert ok, msg
    assert "filesystem fallback" in msg
    assert not untracked_dir.exists()


def test_remove_worktree_reports_failure_when_fallback_cannot_remove_it(
    repo: Path, monkeypatch
) -> None:
    """If even the filesystem fallback can't remove the directory, remove_worktree reports failure
    rather than claiming success."""
    import shared.git.git_utils as git_utils_mod

    untracked_dir = repo.parent / "stuck-worktree"
    untracked_dir.mkdir()

    monkeypatch.setattr(
        git_utils_mod.shutil, "rmtree", lambda *a, **k: None
    )  # no-op: never removes

    ok, msg = remove_worktree(repo, untracked_dir)

    assert not ok
    assert "Failed to remove worktree" in msg


def test_prune_worktrees_reports_git_failure(repo: Path, monkeypatch) -> None:
    import shared.git.git_utils as git_utils_mod

    monkeypatch.setattr(git_utils_mod, "_run_git", lambda *a, **k: (1, "boom"))

    ok, msg = prune_worktrees(repo)

    assert not ok
    assert "git worktree prune failed" in msg


def test_multiple_worktrees_have_independent_feature_branches(repo: Path) -> None:
    """Two workers' worktrees can each create and hold a distinct feature
    branch concurrently-in-spirit (sequential here, but proving no shared
    working-tree state leaks between them)."""
    wt_a = repo.parent / "wt-a"
    wt_b = repo.parent / "wt-b"
    add_worktree(repo, wt_a, ref=DEVELOPMENT_BRANCH)
    add_worktree(repo, wt_b, ref=DEVELOPMENT_BRANCH)

    ok_a, branch_a = create_feature_branch(wt_a, DEVELOPMENT_BRANCH, "task-a")
    ok_b, branch_b = create_feature_branch(wt_b, DEVELOPMENT_BRANCH, "task-b")
    assert ok_a and ok_b
    assert branch_a != branch_b

    (wt_a / "a.txt").write_text("a", encoding="utf-8")
    (wt_b / "b.txt").write_text("b", encoding="utf-8")
    assert not (wt_a / "b.txt").exists()
    assert not (wt_b / "a.txt").exists()


def test_create_feature_branch_retry_reuses_branch_already_checked_out_here(repo: Path) -> None:
    """A retry against a worktree that's still on the branch a prior (crashed) attempt created
    reuses it instead of trying to delete it — which would fail in a worktree (deleting the
    branch currently checked out here is refused) after also failing to check out base_branch
    (refused because it's attached in another linked worktree, i.e. the shared checkout)."""
    wt_path = repo.parent / "wt-retry"
    add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    ok, branch = create_feature_branch(wt_path, DEVELOPMENT_BRANCH, "t1-my-task")
    assert ok, branch
    # Simulate a partial implementation attempt that crashed after branch creation, leaving
    # uncommitted work — the worktree is still on the feature branch (never left it).
    (wt_path / "partial.txt").write_text("wip", encoding="utf-8")

    # Retry: repo (self.path) still has `development` checked out throughout.
    ok2, branch2 = create_feature_branch(wt_path, DEVELOPMENT_BRANCH, "t1-my-task")

    assert ok2, branch2
    assert branch2 == branch
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=wt_path, capture_output=True, text=True
    )
    assert result.stdout.strip() == branch
    # The prior attempt's uncommitted file was preserved (committed on the reused branch), not
    # wiped by a delete+recreate.
    result = subprocess.run(
        ["git", "log", "--all", "--name-only", "--pretty=format:"],
        cwd=wt_path,
        capture_output=True,
        text=True,
    )
    assert "partial.txt" in result.stdout


def test_create_feature_branch_stale_elsewhere_still_deletes_and_recreates(repo: Path) -> None:
    """A branch that exists but is NOT checked out in this path (a genuinely stale leftover, e.g.
    from an earlier job run against the same clone) still goes through the delete+recreate path —
    the reuse short-circuit must not mask this case."""
    ok, branch = create_feature_branch(repo, DEVELOPMENT_BRANCH, "t2-stale")
    assert ok, branch
    checkout_branch(repo, DEVELOPMENT_BRANCH)  # leave the branch (repo is on development again)

    ok2, branch2 = create_feature_branch(repo, DEVELOPMENT_BRANCH, "t2-stale")

    assert ok2, branch2
    assert branch2 == branch
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True
    )
    assert result.stdout.strip() == branch  # recreated and checked out fresh


def test_create_feature_branch_fails_honestly_when_branch_owned_by_another_worktree(
    repo: Path,
) -> None:
    """A branch actively checked out in a DIFFERENT worktree (e.g. another worker still owns it)
    cannot be recovered from here — git refuses to delete a branch checked out anywhere. This must
    fail with a clear message, not silently succeed or hang trying to attach development first."""
    owner_wt = repo.parent / "wt-owner"
    add_worktree(repo, owner_wt, ref=DEVELOPMENT_BRANCH)
    ok, branch = create_feature_branch(owner_wt, DEVELOPMENT_BRANCH, "t3-shared")
    assert ok, branch
    # owner_wt is still checked out on `branch` — never left it.

    other_wt = repo.parent / "wt-other"
    add_worktree(repo, other_wt, ref=DEVELOPMENT_BRANCH)

    ok2, msg2 = create_feature_branch(other_wt, DEVELOPMENT_BRANCH, "t3-shared")

    assert not ok2
    assert "Failed to delete stale branch" in msg2
    # Neither worktree's state was corrupted by the failed attempt.
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=owner_wt, capture_output=True, text=True
    )
    assert result.stdout.strip() == branch


def test_reset_hard_to_discards_commits_unique_to_current_branch(repo: Path) -> None:
    ok, branch = create_feature_branch(repo, DEVELOPMENT_BRANCH, "t4-reset")
    assert ok, branch
    (repo / "rejected.txt").write_text("stale attempt", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "rejected attempt"], cwd=repo, capture_output=True, check=True)
    assert (repo / "rejected.txt").exists()

    ok2, msg2 = reset_hard_to(repo, DEVELOPMENT_BRANCH)

    assert ok2, msg2
    # Still on the feature branch (reset never switches branches) but its tip and
    # working tree now exactly match development's -- the rejected commit is gone.
    result = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True)
    assert result.stdout.strip() == branch
    assert not (repo / "rejected.txt").exists()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    dev_head = subprocess.run(
        ["git", "rev-parse", DEVELOPMENT_BRANCH], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    assert head == dev_head


def test_reset_hard_to_works_in_worktree_while_ref_checked_out_elsewhere(repo: Path) -> None:
    """The crux claim this helper exists for: resetting to ``development`` from a linked
    worktree must succeed even while ``development`` is attached (checked out) at the main
    repo path -- unlike ``checkout_branch(path, DEVELOPMENT_BRANCH)``, which git refuses."""
    wt_path = repo.parent / "wt-reset"
    add_worktree(repo, wt_path, ref=DEVELOPMENT_BRANCH)
    ok, branch = create_feature_branch(wt_path, DEVELOPMENT_BRANCH, "t5-reset-wt")
    assert ok, branch
    (wt_path / "rejected.txt").write_text("stale attempt", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=wt_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "rejected attempt"], cwd=wt_path, capture_output=True, check=True)

    # development is STILL checked out at repo while we reset in the worktree.
    ok2, msg2 = reset_hard_to(wt_path, DEVELOPMENT_BRANCH)

    assert ok2, msg2
    assert not (wt_path / "rejected.txt").exists()
    # repo's own checkout is untouched.
    result = subprocess.run(["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True)
    assert result.stdout.strip() == DEVELOPMENT_BRANCH


def test_reset_hard_to_removes_untracked_files(repo: Path) -> None:
    """git reset --hard only touches tracked files; a caller-run tool (e.g. a validation
    dry-run that shells out to a package/build tool) can leave untracked files behind
    that survive a bare reset and later get swept into an unrelated commit by a
    subsequent `git add -A`. reset_hard_to must clean those up too."""
    ok, branch = create_feature_branch(repo, DEVELOPMENT_BRANCH, "t6-reset-clean")
    assert ok, branch
    (repo / "untracked.lock").write_text("leftover from a validation tool", encoding="utf-8")
    assert (repo / "untracked.lock").exists()

    ok2, msg2 = reset_hard_to(repo, DEVELOPMENT_BRANCH)

    assert ok2, msg2
    assert not (repo / "untracked.lock").exists()


def test_clean_untracked_files_removes_untracked_but_preserves_tracked(repo: Path) -> None:
    (repo / "tracked.txt").write_text("committed content", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "add tracked file"], cwd=repo, capture_output=True, check=True)
    (repo / "untracked.lock").write_text("leftover from a validation tool", encoding="utf-8")

    ok, msg = clean_untracked_files(repo)

    assert ok, msg
    assert not (repo / "untracked.lock").exists()
    assert (repo / "tracked.txt").exists()


def test_clean_untracked_files_fails_honestly_for_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    ok, msg = clean_untracked_files(not_a_repo)
    assert not ok
    assert "Not a git repository" in msg


def test_reset_hard_to_fails_honestly_for_non_repo(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    ok, msg = reset_hard_to(not_a_repo, DEVELOPMENT_BRANCH)
    assert not ok
    assert "Not a git repository" in msg


def test_reset_hard_to_fails_honestly_for_unresolvable_ref(repo: Path) -> None:
    ok, msg = reset_hard_to(repo, "does-not-exist")
    assert not ok
    assert "Failed to reset" in msg
