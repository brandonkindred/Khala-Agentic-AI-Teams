"""Tests for the shared clone-workspace filesystem conventions."""

from __future__ import annotations

from pathlib import Path

from coding_team.clone_workspace import clone_lock_path


def test_clone_lock_path_is_sibling_of_checkout() -> None:
    # The lock lives beside the checkout (in its parent), not inside it, so it
    # survives the post-success rmtree of the checkout directory.
    lock = clone_lock_path("/cache/github_workspaces/acme/widget/issue-7")
    assert lock == Path("/cache/github_workspaces/acme/widget/.issue-7.clone.lock")


def test_clone_lock_path_accepts_path_and_str() -> None:
    p = Path("/work/acme_widget/issue-9")
    assert clone_lock_path(p) == clone_lock_path(str(p))


def test_clone_lock_path_distinct_issues_distinct_locks() -> None:
    a = clone_lock_path("/cache/gh/o/r/issue-1")
    b = clone_lock_path("/cache/gh/o/r/issue-2")
    assert a != b
    assert a.parent == b.parent
