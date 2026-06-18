"""Tests for the shared clone-workspace filesystem conventions."""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_team.clone_workspace import (
    agent_cache_dir,
    clone_lock_path,
    ephemeral_workspace_roots,
    is_within_ephemeral_workspace,
)


def test_agent_cache_dir_default(monkeypatch) -> None:
    """agent_cache_dir returns the '.agent_cache' default when AGENT_CACHE is unset."""
    monkeypatch.delenv("AGENT_CACHE", raising=False)
    assert agent_cache_dir() == ".agent_cache"


def test_agent_cache_dir_blank_falls_back(monkeypatch) -> None:
    """A whitespace-only AGENT_CACHE is treated as unset and falls back to the default."""
    monkeypatch.setenv("AGENT_CACHE", "   ")
    assert agent_cache_dir() == ".agent_cache"


def test_agent_cache_dir_value_is_stripped(monkeypatch) -> None:
    """agent_cache_dir strips surrounding whitespace from the AGENT_CACHE value."""
    monkeypatch.setenv("AGENT_CACHE", "  /cache  ")
    assert agent_cache_dir() == "/cache"


def test_clone_lock_path_is_sibling_of_checkout() -> None:
    """The lock lives beside the checkout (in its parent), so it survives the post-success rmtree."""
    lock = clone_lock_path("/cache/github_workspaces/acme/widget/issue-7")
    assert lock == Path("/cache/github_workspaces/acme/widget/.issue-7.clone.lock")


def test_clone_lock_path_accepts_path_and_str() -> None:
    """clone_lock_path accepts either a str or a Path and yields the same result."""
    p = Path("/work/acme_widget/issue-9")
    assert clone_lock_path(p) == clone_lock_path(str(p))


def test_clone_lock_path_distinct_issues_distinct_locks() -> None:
    """Distinct issue checkouts get distinct lock files in the same parent."""
    a = clone_lock_path("/cache/gh/o/r/issue-1")
    b = clone_lock_path("/cache/gh/o/r/issue-2")
    assert a != b
    assert a.parent == b.parent


@pytest.mark.parametrize(
    "name,expected",
    [
        ("issue-7", True),
        ("issue-12345", True),
        ("issue-", False),
        ("issue-7a", False),
        ("issue-7/extra", False),
        ("acme_widget", False),
        ("widget", False),
        ("", False),
    ],
)
def test_is_per_issue_dir(name, expected) -> None:
    """is_per_issue_dir matches exactly the auto-derived 'issue-<digits>' shape and nothing broader."""
    from coding_team.clone_workspace import is_per_issue_dir

    assert is_per_issue_dir(name) is expected


@pytest.mark.parametrize("root", ["/", ""])
def test_clone_lock_path_rejects_empty_final_component(root) -> None:
    """A filesystem root has no final component, so clone_lock_path raises rather than emit '/.clone.lock'."""
    with pytest.raises(ValueError):
        clone_lock_path(root)


# ---------------------------------------------------------------------------
# ephemeral_workspace_roots
# ---------------------------------------------------------------------------


def test_ephemeral_roots_default_is_agent_cache_github_workspaces(monkeypatch) -> None:
    """With only AGENT_CACHE set, the single ephemeral root is '<cache>/github_workspaces'."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    roots = ephemeral_workspace_roots()
    assert roots == [Path("/cache/github_workspaces").resolve()]


def test_ephemeral_roots_blank_agent_cache_falls_back(monkeypatch) -> None:
    """A blank AGENT_CACHE falls back to the '.agent_cache/github_workspaces' default root."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "   ")  # whitespace → treated as unset
    roots = ephemeral_workspace_roots()
    assert roots == [(Path(".agent_cache") / "github_workspaces").resolve()]


def test_ephemeral_roots_includes_workspace_roots_in_order(monkeypatch) -> None:
    """Workspace-root env vars contribute roots ahead of the AGENT_CACHE root, in declaration order."""
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
    """SE_WORKSPACE_DIR and WORKSPACE_ROOT pointing at the same dir collapse to one root."""
    monkeypatch.setenv("SE_WORKSPACE_DIR", "/same")
    monkeypatch.setenv("WORKSPACE_ROOT", "/same")
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    roots = ephemeral_workspace_roots()
    assert roots == [Path("/same").resolve(), Path("/cache/github_workspaces").resolve()]


# ---------------------------------------------------------------------------
# is_within_ephemeral_workspace
# ---------------------------------------------------------------------------


def test_is_within_true_for_path_under_root(monkeypatch) -> None:
    """A per-issue path under the AGENT_CACHE github_workspaces root is recognised as within."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    assert is_within_ephemeral_workspace("/cache/github_workspaces/acme/widget/issue-7") is True


def test_is_within_true_under_workspace_root(monkeypatch) -> None:
    """A per-issue path under a WORKSPACE_ROOT is recognised as within."""
    monkeypatch.setenv("WORKSPACE_ROOT", "/ws")
    assert is_within_ephemeral_workspace("/ws/acme_widget/issue-3") is True


def test_is_within_false_outside_any_root(monkeypatch) -> None:
    """A path outside every configured ephemeral root is not within."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    assert is_within_ephemeral_workspace("/somewhere/else/issue-7") is False


def test_is_within_false_for_root_itself(monkeypatch) -> None:
    """The root itself is excluded (a strict descendant is required; the root is not a checkout)."""
    monkeypatch.delenv("SE_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.setenv("AGENT_CACHE", "/cache")
    assert is_within_ephemeral_workspace("/cache/github_workspaces") is False


def test_is_within_false_for_unresolvable_path(monkeypatch) -> None:
    """An unresolvable path (embedded null byte makes resolve() raise) is treated as not within."""
    monkeypatch.setenv("WORKSPACE_ROOT", "/ws")
    assert is_within_ephemeral_workspace("/ws/bad\x00path") is False
