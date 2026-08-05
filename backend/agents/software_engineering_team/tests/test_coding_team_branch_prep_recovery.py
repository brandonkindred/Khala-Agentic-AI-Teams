"""
Restart recovery & continuation tests for coding-team branch prep.

Every test runs against real on-disk git repositories in an identity-free
environment (no global git config, no identity env vars) so the suite
reproduces the agent container regardless of the developer's machine. The
"remote" is a sibling local repo added as `origin`.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional

import pytest

from software_engineering_team.tests.conftest import (
    _ensure_real_modules,
    _stub_orchestrator_only,
)


@pytest.fixture
def api(monkeypatch):
    _ensure_real_modules()
    _stub_orchestrator_only(monkeypatch)
    from software_engineering_team.api import coding_team_main as api_main

    return api_main


@pytest.fixture
def identity_free_env(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / "gitconfig-missing"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for var in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "GIT_COMMIT_USER_NAME",
        "GIT_COMMIT_USER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(var, raising=False)


def _run(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)


def _must(cwd: str, *args: str) -> str:
    r = _run(cwd, *args)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr or r.stdout}"
    return r.stdout.strip()


def _identity_env() -> dict:
    """Identity for *test fixture* commits that simulate an interrupted run."""
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.com",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.com",
    }


def _commit_file(repo: str, relpath: str, content: str, message: str) -> None:
    path = os.path.join(repo, relpath)
    os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    _must(repo, "add", "-A")
    r = subprocess.run(
        ["git", "-C", repo, "commit", "-q", "--no-gpg-sign", "-m", message],
        capture_output=True,
        text=True,
        env=_identity_env(),
    )
    assert r.returncode == 0, r.stderr


@pytest.fixture
def repo_pair(tmp_path, identity_free_env):
    """(remote_path, clone_path): remote has one commit on main; clone tracks it."""
    remote = str(tmp_path / "remote")
    os.makedirs(remote)
    _must(remote, "init", "-q")
    _must(remote, "config", "commit.gpgsign", "false")
    _must(remote, "checkout", "-q", "-b", "main")
    _commit_file(remote, "README.md", "base\n", "base")
    clone = str(tmp_path / "clone")
    r = subprocess.run(["git", "clone", "-q", remote, clone], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    _must(clone, "config", "commit.gpgsign", "false")
    return remote, clone


def _branch_exists(repo: str, pattern: str) -> Optional[str]:
    out = _must(repo, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{pattern}")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return lines[-1] if lines else None


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------


class TestActiveIssueMarker:
    def test_read_returns_none_when_unset(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._read_active_issue(clone) is None

    def test_write_then_read_roundtrip(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._write_active_issue(clone, 7)
        assert api._read_active_issue(clone) == 7
        assert _must(clone, "config", "--local", "khala.active-issue") == "7"

    def test_clear_removes_marker(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._write_active_issue(clone, 7)
        api._clear_active_issue(clone)
        assert api._read_active_issue(clone) is None

    def test_clear_is_idempotent(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._clear_active_issue(clone)  # never set; must not raise
        assert api._read_active_issue(clone) is None

    def test_read_garbage_value_returns_none(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "config", "--local", "khala.active-issue", "not-a-number")
        assert api._read_active_issue(clone) is None


# ---------------------------------------------------------------------------
# Git-graph helpers
# ---------------------------------------------------------------------------


class TestGraphHelpers:
    def test_is_ahead_false_for_missing_ref(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._is_ahead(clone, "no-such-branch", "origin/main") is False

    def test_is_ahead_false_when_equal(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._is_ahead(clone, "main", "origin/main") is False

    def test_is_ahead_true_with_extra_commit(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(clone, "work.py", "x = 1\n", "progress")
        assert api._is_ahead(clone, "khala/issue-7", "origin/main") is True

    def test_rescue_branch_name_tags_issue_and_avoids_collisions(
        self, api, repo_pair, monkeypatch
    ) -> None:
        _, clone = repo_pair
        monkeypatch.setattr(api, "_utc_timestamp", lambda: "20260101-000000")
        first = api._rescue_branch_name(clone, 5)
        assert first == "khala/rescue/issue-5-20260101-000000"
        _must(clone, "branch", first)
        second = api._rescue_branch_name(clone, 5)
        assert second == "khala/rescue/issue-5-20260101-000000-1"
        untagged = api._rescue_branch_name(clone, None)
        assert untagged == "khala/rescue/20260101-000000"

    def test_latest_issue_rescue_ref_picks_newest(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "branch", "khala/rescue/issue-7-20260101-000000")
        _must(clone, "branch", "khala/rescue/issue-7-20260102-000000")
        _must(clone, "branch", "khala/rescue/issue-9-20260103-000000")
        assert api._latest_issue_rescue_ref(clone, 7) == "khala/rescue/issue-7-20260102-000000"
        assert api._latest_issue_rescue_ref(clone, 4) is None


# ---------------------------------------------------------------------------
# Dirty-tree recovery
# ---------------------------------------------------------------------------


class TestRecoverDirtyTree:
    def test_same_issue_commits_in_place(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "development")
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("in progress\n")
        tip, note, err = api._recover_dirty_tree(clone, 7, 7, "?? wip.txt")
        assert err is None
        assert tip == "development"
        assert "development" in note
        ok, _, listing = api._working_tree_dirty(clone)
        assert ok is True and listing is None
        assert "wip.txt" in _must(clone, "show", "--stat", "--name-only", "development")

    def test_foreign_issue_rescued_to_tagged_branch(self, api, repo_pair) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("other issue\n")
        tip, note, err = api._recover_dirty_tree(clone, 5, 7, "?? wip.txt")
        assert err is None
        assert tip is None  # foreign work is preserved, not a continuation seed
        rescued = _branch_exists(clone, "khala/rescue/issue-5-*")
        assert rescued is not None and rescued in note
        assert "wip.txt" in _must(clone, "show", "--stat", "--name-only", rescued)

    def test_no_marker_rescued_to_untagged_branch(self, api, repo_pair) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("who knows\n")
        tip, note, err = api._recover_dirty_tree(clone, None, 7, "?? wip.txt")
        assert err is None and tip is None
        rescued = _branch_exists(clone, "khala/rescue/2*")
        assert rescued is not None
        assert "issue-" not in rescued

    def test_commit_failure_reported(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        monkeypatch.setattr(api, "commit_working_tree", lambda *_a, **_kw: (False, "boom"))
        tip, note, err = api._recover_dirty_tree(clone, None, 7, "?? wip.txt")
        assert tip is None and note is None
        assert "boom" in err
        # Nothing was deleted: the dirty file is still there.
        assert os.path.exists(os.path.join(clone, "wip.txt"))


# ---------------------------------------------------------------------------
# _prepare_issue_branch: recovery + continuation end to end
# ---------------------------------------------------------------------------


def _prep(api, clone: str, issue: int = 7):
    return api._prepare_issue_branch(
        clone, "origin", "main", f"khala/issue-{issue}", None, issue_number=issue
    )


class TestPrepareIssueBranchRecovery:
    def test_fresh_clean_checkout_unchanged_behavior(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert notes == []
        assert _must(clone, "rev-parse", "--abbrev-ref", "HEAD") == "khala/issue-7"
        assert _must(clone, "rev-parse", "khala/issue-7") == _must(
            clone, "rev-parse", "origin/main"
        )
        assert _branch_exists(clone, "khala/rescue/*") is None

    def test_marker_written_on_success(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        assert api._read_active_issue(clone) == 7

    def test_dirty_same_issue_recovers_and_continues(self, api, repo_pair) -> None:
        """The headline scenario: interrupted mid-run, job deleted, new job started."""
        _, clone = repo_pair
        # Simulate the interrupted run: prep'd for issue 7, progressed on development.
        ok, _, _ = _prep(api, clone)
        assert ok is True
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "src/progress.py", "x = 1\n", "task progress")
        os.makedirs(os.path.join(clone, "specs"), exist_ok=True)
        with open(os.path.join(clone, "specs/notes.md"), "w", encoding="utf-8") as fh:
            fh.write("open questions\n")
        # (no _clear_active_issue: the job died)

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert "src/progress.py" in files
        assert "specs/notes.md" in files
        assert any("Recovered" in n for n in notes)
        assert any("Continuing" in n for n in notes)
        assert api._working_tree_dirty(clone) == (True, False, None)

    def test_dirty_foreign_issue_rescued_not_continued(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone, issue=5)
        assert ok is True
        with open(os.path.join(clone, "foreign.txt"), "w", encoding="utf-8") as fh:
            fh.write("issue 5 work\n")

        ok, err, notes = _prep(api, clone, issue=7)
        assert ok is True, err
        assert "foreign.txt" not in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        rescued = _branch_exists(clone, "khala/rescue/issue-5-*")
        assert rescued is not None
        assert "foreign.txt" in _must(clone, "ls-tree", "-r", "--name-only", rescued)

    def test_status_failure_fails_closed(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        monkeypatch.setattr(
            api, "_working_tree_dirty", lambda p: (False, True, "git status failed")
        )
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "git status failed" in err

    def test_recovery_failure_fails_closed_with_both_messages(
        self, api, repo_pair, monkeypatch
    ) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        monkeypatch.setattr(api, "commit_working_tree", lambda *_a, **_kw: (False, "boom"))
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "uncommitted changes" in err and "boom" in err
        assert os.path.exists(os.path.join(clone, "wip.txt"))


class TestPrepareIssueBranchContinuation:
    def test_continues_from_local_issue_branch(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/issue-7", "origin/main")
        _commit_file(clone, "done.py", "ok = True\n", "finished work")
        _must(clone, "checkout", "-q", "main")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert "done.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert any("Continuing" in n for n in notes)

    def test_continues_from_remote_issue_branch(self, api, repo_pair) -> None:
        remote, clone = repo_pair
        # Previous job pushed khala/issue-7 then died; local checkout knows nothing.
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "pushed.py", "ok = True\n", "pushed work")
        _must(remote, "checkout", "-q", "main")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert "pushed.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert any("Continuing" in n for n in notes)

    def test_continues_from_issue_rescue_ref(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/rescue/issue-7-20260101-000000", "origin/main")
        _commit_file(clone, "rescued.py", "ok = True\n", "rescued work")
        _must(clone, "checkout", "-q", "main")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert "rescued.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")

    def test_local_preferred_when_it_contains_the_remote_tip(self, api, repo_pair) -> None:
        """Local strictly newer than remote (job died after fast-forward but
        before push): continuing locally loses nothing the remote has."""
        remote, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/issue-7", "origin/main")
        _commit_file(clone, "pushed.py", "p = 1\n", "pushed work")
        _must(clone, "push", "-q", "origin", "khala/issue-7")
        _commit_file(clone, "newer.py", "n = 1\n", "local-only newer work")
        _must(clone, "checkout", "-q", "main")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert "pushed.py" in files and "newer.py" in files
        # Nothing needed preserving: the seed contains the remote tip.
        assert _branch_exists(clone, "khala/rescue/*") is None

    def test_stale_local_with_newer_remote_continues_from_remote(self, api, repo_pair) -> None:
        """The eventual publish is `push --force-with-lease` and prep's own
        fetch refreshes the lease — seeding from a stale local tip would let
        the push drop the remote-only commits."""
        remote, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "khala/issue-7", "origin/main")
        _commit_file(clone, "shared.py", "s = 1\n", "shared work")
        _must(clone, "push", "-q", "origin", "khala/issue-7")
        # The remote moves ahead (e.g. another checkout's job pushed more).
        _must(remote, "checkout", "-q", "khala/issue-7")
        _commit_file(remote, "remote_only.py", "r = 1\n", "remote-only progress")
        _must(remote, "checkout", "-q", "main")
        # Local stays at the stale tip.
        _must(clone, "checkout", "-q", "main")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert "shared.py" in files and "remote_only.py" in files

    def test_diverged_local_and_remote_prefers_remote_and_preserves_local(
        self, api, repo_pair
    ) -> None:
        """When the tips diverged, the remote's unique commits would be
        irrecoverably force-pushed away if not seeded from; the local branch's
        unique commits are pinned to a rescue ref instead."""
        remote, clone = repo_pair
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "remote.py", "r = 1\n", "remote work")
        _must(remote, "checkout", "-q", "main")
        _must(clone, "checkout", "-q", "-b", "khala/issue-7", "origin/main")
        _commit_file(clone, "local.py", "l = 1\n", "local work")
        local_tip = _must(clone, "rev-parse", "khala/issue-7")
        _must(clone, "checkout", "-q", "main")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert "remote.py" in files
        assert "local.py" not in files
        # The diverged local tip is still reachable via a rescue ref.
        assert local_tip in _must(clone, "rev-list", "--branches")
        rescued = _branch_exists(clone, "khala/rescue/*")
        assert rescued is not None
        assert "local.py" in _must(clone, "ls-tree", "-r", "--name-only", rescued)

    def test_remote_floor_beats_marker_tip_for_publishable_line(self, api, repo_pair) -> None:
        """The remote floor applies to every candidate, including the marker
        tip: the published line must contain the live remote-only commits
        (the force-with-lease push would otherwise drop them — a local pin is
        no substitute for commits the remote is expected to keep), while the
        local development progress stays reachable on a rescue ref."""
        remote, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        # Interrupted run for issue 7: progress on development, marker left set.
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "dev_progress.py", "d = 1\n", "dev progress")
        dev_tip = _must(clone, "rev-parse", "development")
        # Meanwhile the remote issue branch holds diverged commits.
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "remote_only.py", "r = 1\n", "remote-only progress")
        _must(remote, "checkout", "-q", "main")
        remote_tip = _must(remote, "rev-parse", "khala/issue-7")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        # The remote-only commits are in the publishable line…
        assert "remote_only.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert remote_tip in _must(clone, "rev-list", "--branches")
        # …and the local development progress stays reachable on a rescue ref.
        assert dev_tip in _must(clone, "rev-list", "--branches")
        assert _branch_exists(clone, "khala/rescue/issue-7-*") is not None


class TestOrphanPrevention:
    def test_foreign_development_progress_preserved_before_reset(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone, issue=5)
        assert ok is True
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "issue5.py", "x = 5\n", "issue 5 progress")
        dev_tip = _must(clone, "rev-parse", "development")

        ok, err, _ = _prep(api, clone, issue=7)
        assert ok is True, err
        # development was re-seeded from origin/main…
        assert _must(clone, "rev-parse", "development") == _must(clone, "rev-parse", "origin/main")
        # …but the old tip is still reachable from a rescue ref.
        reachable = _must(clone, "rev-list", "--all")
        assert dev_tip in reachable
        assert _branch_exists(clone, "khala/rescue/issue-5-*") is not None


class TestWorkingTreeDirtyTriple:
    def test_clean(self, api, repo_pair) -> None:
        _, clone = repo_pair
        assert api._working_tree_dirty(clone) == (True, False, None)

    def test_dirty(self, api, repo_pair) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "x.txt"), "w", encoding="utf-8") as fh:
            fh.write("d\n")
        ok, dirty, listing = api._working_tree_dirty(clone)
        assert (ok, dirty) == (True, True)
        assert "x.txt" in listing

    def test_status_failure(self, api, tmp_path) -> None:
        not_a_repo = str(tmp_path / "empty")
        os.makedirs(not_a_repo)
        ok, dirty, listing = api._working_tree_dirty(not_a_repo)
        assert ok is False and dirty is True
        assert listing


# ---------------------------------------------------------------------------
# Failure-matrix branches (fail closed; never destroy work)
# ---------------------------------------------------------------------------


class TestFailureMatrix:
    def test_is_ahead_false_when_rev_list_fails(self, api, monkeypatch) -> None:
        def fake_git(repo_path, *args, timeout=120.0, env=None):
            if args[0] == "rev-parse":
                return 0, "abc123"
            return 1, "rev-list exploded"

        monkeypatch.setattr(api, "_git", fake_git)
        assert api._is_ahead("/repo", "branch", "origin/main") is False

    def test_rescue_branch_name_exhaustion_returns_none(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        monkeypatch.setattr(api, "_utc_timestamp", lambda: "20260101-000000")
        base = "khala/rescue/issue-5-20260101-000000"
        _must(clone, "branch", base)
        for i in range(1, 10):
            _must(clone, "branch", f"{base}-{i}")
        assert api._rescue_branch_name(clone, 5) is None

    def test_recover_same_issue_commit_failure_reported(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        monkeypatch.setattr(api, "commit_working_tree", lambda *_a, **_kw: (False, "in-place boom"))
        tip, note, err = api._recover_dirty_tree(clone, 7, 7, "?? wip.txt")
        assert (tip, note) == (None, None)
        assert "in-place boom" in err
        assert os.path.exists(os.path.join(clone, "wip.txt"))

    def test_recover_rescue_name_exhaustion_fails_closed(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        monkeypatch.setattr(api, "_rescue_branch_name", lambda *_a: None)
        tip, note, err = api._recover_dirty_tree(clone, None, 7, "?? wip.txt")
        assert (tip, note) == (None, None)
        assert "could not allocate" in err

    def test_recover_rescue_branch_creation_failure_fails_closed(
        self, api, repo_pair, monkeypatch
    ) -> None:
        _, clone = repo_pair
        _must(clone, "branch", "khala/rescue/taken")
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        # Simulate a name-allocation race: the allocated name already exists.
        monkeypatch.setattr(api, "_rescue_branch_name", lambda *_a: "khala/rescue/taken")
        tip, note, err = api._recover_dirty_tree(clone, None, 7, "?? wip.txt")
        assert (tip, note) == (None, None)
        assert "rescue branch creation failed" in err
        assert os.path.exists(os.path.join(clone, "wip.txt"))

    def test_preserve_noop_when_tip_reachable_from_seed(self, api, repo_pair) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "development", "origin/main")
        _commit_file(clone, "dev.py", "d = 1\n", "dev work")
        _must(clone, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(clone, "more.py", "m = 1\n", "more work")
        # development is ahead of base but fully contained in the seed.
        err = api._preserve_if_would_orphan(
            clone, "development", "origin/main", "khala/issue-7", None
        )
        assert err is None
        assert _branch_exists(clone, "khala/rescue/*") is None

    def test_preserve_name_exhaustion_is_an_error(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "development", "origin/main")
        _commit_file(clone, "dev.py", "d = 1\n", "dev work")
        _must(clone, "checkout", "-q", "main")
        monkeypatch.setattr(api, "_rescue_branch_name", lambda *_a: None)
        err = api._preserve_if_would_orphan(
            clone, "development", "origin/main", "origin/main", None
        )
        assert "could not allocate" in err

    def test_preserve_branch_command_failure_is_an_error(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        _must(clone, "checkout", "-q", "-b", "development", "origin/main")
        _commit_file(clone, "dev.py", "d = 1\n", "dev work")
        _must(clone, "checkout", "-q", "main")
        _must(clone, "branch", "khala/rescue/taken")
        monkeypatch.setattr(api, "_rescue_branch_name", lambda *_a: "khala/rescue/taken")
        err = api._preserve_if_would_orphan(
            clone, "development", "origin/main", "origin/main", None
        )
        assert "failed to preserve" in err

    def test_prep_fetch_failure_fails_closed(self, api, repo_pair) -> None:
        remote, clone = repo_pair
        import shutil

        shutil.rmtree(remote)
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert err

    def test_prep_preserve_failure_fails_closed(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        monkeypatch.setattr(api, "_preserve_if_would_orphan", lambda *_a, **_kw: "boom-preserve")
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "boom-preserve" in err

    def test_prep_development_checkout_failure_fails_closed(
        self, api, repo_pair, monkeypatch
    ) -> None:
        _, clone = repo_pair
        real_git = api._git

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            if args[:2] == ("checkout", "-B") and args[2] == "development":
                return 1, "dev checkout exploded"
            return real_git(repo_path, *args, timeout=timeout, env=env)

        monkeypatch.setattr(api, "_git", fake_git)
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "dev checkout exploded" in err

    def test_prep_integration_checkout_failure_fails_closed(
        self, api, repo_pair, monkeypatch
    ) -> None:
        _, clone = repo_pair
        real_git = api._git

        def fake_git(repo_path, *args, timeout=120.0, env=None):
            if args[:2] == ("checkout", "-B") and args[2] == "khala/issue-7":
                return 1, "integration checkout exploded"
            return real_git(repo_path, *args, timeout=timeout, env=env)

        monkeypatch.setattr(api, "_git", fake_git)
        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "integration checkout exploded" in err


class TestMarkerLifecycleInPrep:
    """The marker means "this checkout holds unpublished work for issue N".
    It is overwritten only by a fully successful prep and must survive every
    prep failure — the leftovers it attributes (dirt and `development`-ahead
    commits) are still on the checkout until a successful re-seed absorbs
    them, and clearing earlier breaks same-issue continuation on retry."""

    def test_prep_failure_retains_marker_so_retry_continues(self, api, repo_pair) -> None:
        """The reviewer scenario: a same-issue run committed progress on
        development; the retry fails mid-prep (fetch). The marker must
        survive so a later retry still seeds continuation from development."""
        remote, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "progress.py", "x = 1\n", "interrupted progress")
        # The job died; a retry then fails at fetch.
        remote_backup = remote + ".bak"
        os.rename(remote, remote_backup)
        ok, _, _ = _prep(api, clone)
        assert ok is False
        assert api._read_active_issue(clone) == 7

        # Restore the remote: the next retry continues from development.
        os.rename(remote_backup, remote)
        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        assert "progress.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert any("Continuing" in n for n in notes)

    def test_successful_prep_for_other_issue_overwrites_stale_marker(self, api, repo_pair) -> None:
        """A fully successful prep absorbs the previous issue's leftovers
        (rescued/preserved with its tag) and is the one safe transition
        point: the marker switches to the new issue."""
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone, issue=5)
        assert ok is True
        assert api._read_active_issue(clone) == 5
        ok, _, _ = _prep(api, clone, issue=9)
        assert ok is True
        assert api._read_active_issue(clone) == 9

    def test_status_failure_retains_marker_for_attribution(
        self, api, repo_pair, monkeypatch
    ) -> None:
        """Recovery never ran — the marker is still the only attribution for
        whatever is in the tree; it must survive the fail-closed exit."""
        _, clone = repo_pair
        api._write_active_issue(clone, 7)
        monkeypatch.setattr(
            api, "_working_tree_dirty", lambda p: (False, True, "git status failed")
        )
        ok, _, _ = _prep(api, clone, issue=9)
        assert ok is False
        assert api._read_active_issue(clone) == 7

    def test_recovery_failure_retains_marker_for_attribution(
        self, api, repo_pair, monkeypatch
    ) -> None:
        """Dirt is still on disk after a failed recovery commit; the marker
        must stay so the retry can attribute it."""
        _, clone = repo_pair
        api._write_active_issue(clone, 7)
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("x\n")
        monkeypatch.setattr(api, "commit_working_tree", lambda *_a, **_kw: (False, "boom"))
        ok, _, _ = _prep(api, clone, issue=9)
        assert ok is False
        assert api._read_active_issue(clone) == 7


class TestGitOutputCredentialScrubbing:
    """Transient auth material (the Basic header) must never reach job errors
    or issue comments, even via verbose/trace git output."""

    def test_git_scrubs_header_credential_from_output(self, api, monkeypatch) -> None:
        auth_env = api._git_auth_env("tok-secret")
        header_value = auth_env["GIT_CONFIG_VALUE_0"]  # "Authorization: Basic <b64>"
        encoded = header_value.rsplit(" ", 1)[-1]

        class _Proc:
            returncode = 1
            stdout = ""
            stderr = f"trace: http.extraHeader: {header_value} rejected ({encoded})"

        monkeypatch.setattr(api.subprocess, "run", lambda *a, **kw: _Proc())
        rc, msg = api._git("/repo", "fetch", "--", "origin", "main", env=auth_env)
        assert rc == 1
        assert encoded not in msg
        assert header_value not in msg
        assert "***" in msg

    def test_git_without_auth_env_output_unchanged(self, api, monkeypatch) -> None:
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "fatal: ordinary failure"

        monkeypatch.setattr(api.subprocess, "run", lambda *a, **kw: _Proc())
        rc, msg = api._git("/repo", "status")
        assert rc == 1
        assert msg == "fatal: ordinary failure"


class TestRescueTagAttribution:
    """Preserved work must carry the tag of the issue it belongs to, so
    _latest_issue_rescue_ref can offer it as a continuation candidate later."""

    def test_diverged_local_issue_branch_rescue_is_issue_tagged(self, api, repo_pair) -> None:
        """A khala/issue-N branch is issue-N work by construction — its rescue
        ref must be issue-N-tagged even when the marker is absent/foreign."""
        remote, clone = repo_pair
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "remote.py", "r = 1\n", "remote work")
        _must(remote, "checkout", "-q", "main")
        _must(clone, "checkout", "-q", "-b", "khala/issue-7", "origin/main")
        _commit_file(clone, "local.py", "l = 1\n", "local work")
        _must(clone, "checkout", "-q", "main")
        assert api._read_active_issue(clone) is None  # no marker at all

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        rescued = _branch_exists(clone, "khala/rescue/issue-7-*")
        assert rescued is not None
        assert "local.py" in _must(clone, "ls-tree", "-r", "--name-only", rescued)
        # And the continuation lookup for issue 7 can actually find it.
        assert api._latest_issue_rescue_ref(clone, 7) == rescued

    def test_foreign_development_rescue_keeps_marker_tag(self, api, repo_pair) -> None:
        """development work is attributable only via the marker — unchanged."""
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone, issue=5)
        assert ok is True
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "issue5.py", "x = 5\n", "issue 5 progress")

        ok, err, _ = _prep(api, clone, issue=7)
        assert ok is True, err
        assert _branch_exists(clone, "khala/rescue/issue-5-*") is not None


class TestScopedMarkerClear:
    """Two different issues may run against the same checkout; a job must
    only clear its own marker, never a sibling's."""

    def test_clear_if_matches_only_clears_own_marker(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._write_active_issue(clone, 9)
        api._clear_active_issue_if_matches(clone, 5)  # older job publishing
        assert api._read_active_issue(clone) == 9  # newer job's marker survives
        api._clear_active_issue_if_matches(clone, 9)
        assert api._read_active_issue(clone) is None

    def test_publish_clear_is_scoped_to_the_jobs_issue(self, api, repo_pair) -> None:
        _, clone = repo_pair
        api._clear_active_issue_if_matches(clone, 5)  # no marker set: no-op, no raise
        assert api._read_active_issue(clone) is None


class TestStaleRemoteTrackingRef:
    """A deleted remote issue branch must not be resurrected: the failed
    fetch leaves a stale origin/khala/issue-N tracking ref behind, and
    seeding from it would let the final force push republish commits that
    no longer exist on the remote."""

    def test_stale_tracking_ref_not_used_as_seed(self, api, repo_pair) -> None:
        _, clone = repo_pair
        # Fabricate the post-deletion state: a stale remote-tracking ref
        # pointing at commits the remote no longer has.
        _must(clone, "checkout", "-q", "-b", "tmp", "origin/main")
        _commit_file(clone, "stale.py", "s = 1\n", "stale remote work")
        stale_tip = _must(clone, "rev-parse", "tmp")
        _must(clone, "update-ref", "refs/remotes/origin/khala/issue-7", stale_tip)
        _must(clone, "checkout", "-q", "main")
        _must(clone, "branch", "-D", "tmp")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        # Fresh seed from base — the stale ref must not have been chosen…
        assert _must(clone, "rev-parse", "khala/issue-7") == _must(
            clone, "rev-parse", "origin/main"
        )
        # …the stale tracking ref is gone so later preps can't trip on it…
        r = _run(clone, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/khala/issue-7")
        assert r.returncode != 0
        # …and its commits remain locally reachable (never-lose-work invariant)
        # on an UNTAGGED rescue ref: a remote deletion is an explicit signal
        # not to continue, so the preserved state must not become an
        # automatic continuation candidate.
        assert stale_tip in _must(clone, "rev-list", "--branches")
        assert _branch_exists(clone, "khala/rescue/issue-7-*") is None
        assert _branch_exists(clone, "khala/rescue/2*") is not None
        assert api._latest_issue_rescue_ref(clone, 7) is None

    def test_live_remote_branch_still_fetched_and_used(self, api, repo_pair) -> None:
        """Guard: the stale-ref handling must not regress live-remote continuation."""
        remote, clone = repo_pair
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "pushed.py", "ok = True\n", "pushed work")
        _must(remote, "checkout", "-q", "main")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        assert "pushed.py" in _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")


class TestSameIssueContinuationPicksRichestTip:
    """When the interrupted run's HEAD was not on development, the wip commit
    lands elsewhere — development's merged progress must still drive the seed
    instead of being demoted to a rescue ref by a 1-commit wip tip."""

    def test_dev_progress_wins_when_head_crashed_off_development(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        # Orchestrator merged real work into development…
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "progress1.py", "a = 1\n", "task 1 merged")
        _commit_file(clone, "progress2.py", "b = 2\n", "task 2 merged")
        # …then a later step left HEAD on the integration branch (still at
        # base) and crashed with a dirty tree.
        _must(clone, "checkout", "-q", "khala/issue-7")
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("partial\n")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        # The merged development progress is the continuation line…
        assert "progress1.py" in files and "progress2.py" in files
        # …and the diverged wip commit stays reachable on a local ref.
        assert _branch_exists(clone, "khala/rescue/issue-7-*") is not None

    def test_wip_tip_wins_when_it_contains_development(self, api, repo_pair) -> None:
        """HEAD on development at crash time (the common case) is unchanged."""
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "progress.py", "a = 1\n", "merged work")
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("partial\n")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        assert "progress.py" in files and "wip.txt" in files


class TestRemoteFloorOnSeedSelection:
    """No seed choice may let the final force-with-lease push (lease refreshed
    by prep's own fetch) drop commits that exist only on the live remote
    issue branch: any candidate that does not contain the live remote tip is
    ineligible."""

    def test_marker_tip_skipped_when_it_lacks_remote_only_commits(self, api, repo_pair) -> None:
        remote, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        # Interrupted local run: progress on development, marker retained.
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "local_only.py", "l = 1\n", "local progress")
        # Meanwhile the remote issue branch gained commits this checkout never saw.
        _must(remote, "checkout", "-q", "-b", "khala/issue-7")
        _commit_file(remote, "remote_only.py", "r = 1\n", "remote-only progress")
        _must(remote, "checkout", "-q", "main")

        ok, err, _ = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        # The publishable line must contain the remote-only commits…
        assert "remote_only.py" in files
        # …and the local development progress stays reachable locally.
        rescued = _branch_exists(clone, "khala/rescue/issue-7-*")
        assert rescued is not None
        assert "local_only.py" in _must(clone, "ls-tree", "-r", "--name-only", rescued)


class TestIssueBranchFetchFailureHandling:
    """Only a CONFIRMED-absent remote issue branch may take the stale-ref
    deletion path. `git fetch` exit codes do not distinguish "no such remote
    ref" from a transient transport failure, and deleting the tracking ref on
    a network blip would hide live remote progress from candidate selection —
    prep must fail closed instead so a retry sees the real remote state."""

    @staticmethod
    def _stale_tracking_ref(clone: str) -> str:
        """Plant refs/remotes/origin/khala/issue-7 at a local-only commit."""
        _must(clone, "checkout", "-q", "-b", "tmp", "origin/main")
        _commit_file(clone, "remote_work.py", "r = 1\n", "remote work")
        tip = _must(clone, "rev-parse", "tmp")
        _must(clone, "update-ref", "refs/remotes/origin/khala/issue-7", tip)
        _must(clone, "checkout", "-q", "main")
        _must(clone, "branch", "-D", "tmp")
        return tip

    def test_transient_fetch_failure_fails_closed(self, api, repo_pair, monkeypatch) -> None:
        """Fetch fails but the probe confirms the branch is alive remotely:
        prep must surface the fetch error, not discard the tracking ref."""
        _, clone = repo_pair
        tip = self._stale_tracking_ref(clone)

        real_git = api._git

        def fake_git(repo_path, *args, **kwargs):
            if args[0] == "fetch" and "khala/issue-7" in args:
                return 1, "transient network error"
            if args[0] == "ls-remote":
                return 0, f"{tip}\trefs/heads/khala/issue-7"
            return real_git(repo_path, *args, **kwargs)

        monkeypatch.setattr(api, "_git", fake_git)

        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "transient network error" in (err or "")
        # The tracking ref survives untouched and nothing was rescued.
        r = _run(clone, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/khala/issue-7")
        assert r.returncode == 0
        assert _branch_exists(clone, "khala/rescue/*") is None

    def test_unverifiable_absence_fails_closed(self, api, repo_pair, monkeypatch) -> None:
        """Fetch fails and the absence probe also fails: remote state is
        unknowable, so prep must fail rather than guess "absent"."""
        _, clone = repo_pair
        self._stale_tracking_ref(clone)

        real_git = api._git

        def fake_git(repo_path, *args, **kwargs):
            if args[0] == "fetch" and "khala/issue-7" in args:
                return 1, "transient network error"
            if args[0] == "ls-remote":
                return 128, "connection refused"
            return real_git(repo_path, *args, **kwargs)

        monkeypatch.setattr(api, "_git", fake_git)

        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "connection refused" in (err or "")
        r = _run(clone, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/khala/issue-7")
        assert r.returncode == 0
        assert _branch_exists(clone, "khala/rescue/*") is None


class TestDivergedWipReachesContinuationLine:
    """Recovery reports same-issue WIP as continuation state, so it must reach
    the resumed integration line: when the branch the WIP was committed on
    diverged from the chosen seed, prep merges it in (clean case) or leaves it
    on its branch with an explicit operator note (conflict case) — never
    silently stranded on a side branch the orchestrator will not read."""

    def test_diverged_feature_wip_merged_into_seed(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        # Feature branch cut from development, then other work merged into
        # development (the branches diverge)…
        _must(clone, "checkout", "-q", "-b", "feature/t2", "development")
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "merged.py", "m = 1\n", "task 1 merged")
        # …and the crash leaves uncommitted WIP on the feature branch.
        _must(clone, "checkout", "-q", "feature/t2")
        with open(os.path.join(clone, "wip.py"), "w", encoding="utf-8") as fh:
            fh.write("w = 1\n")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        files = _must(clone, "ls-tree", "-r", "--name-only", "khala/issue-7")
        # Both the merged development progress AND the recovered WIP are in
        # the continuation line.
        assert "merged.py" in files
        assert "wip.py" in files
        ok_status, dirty, _ = api._working_tree_dirty(clone)
        assert ok_status is True and dirty is False

    def test_conflicting_wip_left_on_branch_with_note(self, api, repo_pair) -> None:
        _, clone = repo_pair
        ok, _, _ = _prep(api, clone)
        assert ok is True
        _must(clone, "checkout", "-q", "-b", "feature/t2", "development")
        _must(clone, "checkout", "-q", "development")
        _commit_file(clone, "same.py", "dev = 1\n", "dev version")
        # WIP adds the same file with different content → add/add conflict.
        _must(clone, "checkout", "-q", "feature/t2")
        with open(os.path.join(clone, "same.py"), "w", encoding="utf-8") as fh:
            fh.write("wip = 2\n")

        ok, err, notes = _prep(api, clone)
        assert ok is True, err
        # The continuation line carries development's version…
        assert _must(clone, "show", "khala/issue-7:same.py") == "dev = 1"
        # …the working tree is clean (the failed merge was aborted)…
        ok_status, dirty, _ = api._working_tree_dirty(clone)
        assert ok_status is True and dirty is False
        # …the WIP stays reachable on its own branch…
        assert "same.py" in _must(clone, "ls-tree", "-r", "--name-only", "feature/t2")
        # …and the operator is told it needs manual integration.
        assert any("merge" in n.lower() for n in notes)


class TestRefValidationBeforeRecovery:
    """A request that can never proceed (unsafe ref names) must be rejected
    before any recovery side effect: the pre-recovery dirty-tree guard failed
    without mutating the checkout, and recovery must not regress that for
    invalid input — no WIP commit, no rescue branch, no branch switch."""

    @pytest.mark.parametrize(
        "default_branch,integration_branch",
        [("main", "--upload-pack=evil"), ("-evil", "khala/issue-7")],
    )
    def test_unsafe_ref_rejected_before_dirty_tree_recovery(
        self, api, repo_pair, default_branch, integration_branch
    ) -> None:
        _, clone = repo_pair
        api._write_active_issue(clone, 7)
        with open(os.path.join(clone, "wip.txt"), "w", encoding="utf-8") as fh:
            fh.write("uncommitted\n")
        head_before = _must(clone, "rev-parse", "HEAD")

        ok, err, _ = api._prepare_issue_branch(
            clone, "origin", default_branch, integration_branch, None, issue_number=7
        )
        assert ok is False
        assert "unsafe" in (err or "")
        # No mutation happened: tree still dirty, HEAD unmoved, no rescue.
        _, dirty, _ = api._working_tree_dirty(clone)
        assert dirty is True
        assert _must(clone, "rev-parse", "HEAD") == head_before
        assert _must(clone, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert _branch_exists(clone, "khala/rescue/*") is None


class TestStaleRefDeletionFailure:
    """The confirmed-deleted-remote path must verify the stale tracking ref is
    actually gone: a locked/undeletable ref that survives would re-enter
    candidate selection and re-anchor the remote floor to a state the remote
    deliberately deleted."""

    def test_failed_stale_ref_deletion_fails_closed(self, api, repo_pair, monkeypatch) -> None:
        _, clone = repo_pair
        TestIssueBranchFetchFailureHandling._stale_tracking_ref(clone)

        real_git = api._git

        def fake_git(repo_path, *args, **kwargs):
            if args[0] == "update-ref" and "-d" in args:
                return 1, "ref locked"
            return real_git(repo_path, *args, **kwargs)

        monkeypatch.setattr(api, "_git", fake_git)

        ok, err, _ = _prep(api, clone)
        assert ok is False
        assert "stale remote-tracking ref" in (err or "")
        # The surviving ref was reported, not silently used as a candidate.
        r = _run(clone, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/khala/issue-7")
        assert r.returncode == 0
