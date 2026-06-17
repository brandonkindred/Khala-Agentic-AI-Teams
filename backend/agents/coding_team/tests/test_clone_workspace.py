"""Tests for the shared clone-workspace filesystem conventions."""

from __future__ import annotations

from pathlib import Path

from coding_team.clone_workspace import (
    clone_lock_path,
    ephemeral_workspace_roots,
    is_within_ephemeral_workspace,
)


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


# ---------------------------------------------------------------------------
# ephemeral_workspace_roots
# ---------------------------------------------------------------------------


def test_ephemeral_roots_default_is_agent_cache_github_workspaces(monkeypatch) -> None:
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    roots = ephemeral_workspace_roots()
    assert roots == [Path("/cache/github_workspaces").resolve()]


def test_ephemeral_roots_blank_agent_cache_falls_back(monkeypatch) -> None:
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "   ")  # whitespace → treated as unset
    roots = ephemeral_workspace_roots()
    assert roots == [(Path(".agent_cache") / "github_workspaces").resolve()]


def test_ephemeral_roots_includes_workspace_roots_in_order(monkeypatch) -> None:
    monkeypatch.setenv("SE_WORKSPACE_DIR", "/se")
    monkeypatch.setenv("WORKSPACE_ROOT", "/ws")
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    roots = ephemeral_workspace_roots()
    assert roots == [
        Path("/se").resolve(),
        Path("/ws").resolve(),
        Path("/cache/github_workspaces").resolve(),
    ]


def test_ephemeral_roots_deduplicates(monkeypatch) -> None:
    # SE_WORKSPACE_DIR and WORKSPACE_ROOT pointing at the same dir collapse to one.
    monkeypatch.setenv("SE_WORKSPACE_DIR", "/same")
    monkeypatch.setenv("WORKSPACE_ROOT", "/same")
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    roots = ephemeral_workspace_roots()
    assert roots == [Path("/same").resolve(), Path("/cache/github_workspaces").resolve()]


# ---------------------------------------------------------------------------
# is_within_ephemeral_workspace
# ---------------------------------------------------------------------------


def test_is_within_true_for_path_under_root(monkeypatch) -> None:
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    assert is_within_ephemeral_workspace("/cache/github_workspaces/acme/widget/issue-7") is True


def test_is_within_true_under_workspace_root(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", "/ws")
    assert is_within_ephemeral_workspace("/ws/acme_widget/issue-3") is True


def test_is_within_false_outside_any_root(monkeypatch) -> None:
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    assert is_within_ephemeral_workspace("/somewhere/else/issue-7") is False


def test_is_within_false_for_root_itself(monkeypatch) -> None:
    # The root is excluded (strict descendant required); the root is not a checkout.
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    assert is_within_ephemeral_workspace("/cache/github_workspaces") is False


def test_is_within_false_for_unresolvable_path(monkeypatch) -> None:
    monkeypatch.setenv("WORKSPACE_ROOT", "/ws")
    # An embedded null byte makes Path.resolve() raise ValueError → False.
    assert is_within_ephemeral_workspace("/ws/bad\x00path") is False
