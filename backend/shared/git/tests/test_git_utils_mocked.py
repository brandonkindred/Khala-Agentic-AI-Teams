"""Mocked command-building tests for ``shared.git.git_utils``.

Patches ``_run_git`` to assert on the exact subprocess command built, rather
than exercising real git subprocess semantics (see ``test_git_utils.py`` for
that) -- appropriate here because the behavior under test is which flag a
thin wrapper function chooses to pass, not git's own checkout/branch-delete
semantics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shared.git import git_utils


@pytest.fixture
def _fake_git_repo(tmp_path: Path) -> Path:
    """Tmp path that ``looks like`` a git repo (.git/ exists) without being one."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_checkout_branch_default_uses_plain_checkout(monkeypatch, _fake_git_repo) -> None:
    captured_cmds = []
    monkeypatch.setattr(
        git_utils,
        "_run_git",
        lambda path, cmd, *a, **kw: (captured_cmds.append(cmd), (0, ""))[1],
    )
    git_utils.checkout_branch(_fake_git_repo, "development")
    assert captured_cmds[-1] == ["git", "checkout", "development"]


def test_checkout_branch_force_uses_force_flag(monkeypatch, _fake_git_repo) -> None:
    """force=True must add ``-f`` so uncommitted tracked-file changes don't
    block restoring to a known-good branch during cleanup."""
    captured_cmds = []
    monkeypatch.setattr(
        git_utils,
        "_run_git",
        lambda path, cmd, *a, **kw: (captured_cmds.append(cmd), (0, ""))[1],
    )
    ok, _ = git_utils.checkout_branch(_fake_git_repo, "development", force=True)
    assert ok is True
    assert captured_cmds[-1] == ["git", "checkout", "-f", "development"]


def test_delete_branch_default_uses_safe_delete_flag(monkeypatch, _fake_git_repo) -> None:
    """Default (force=False) must use ``-d``, which git refuses for unmerged branches."""
    captured_cmds = []
    monkeypatch.setattr(
        git_utils,
        "_run_git",
        lambda path, cmd, *a, **kw: (captured_cmds.append(cmd), (0, ""))[1],
    )
    git_utils.delete_branch(_fake_git_repo, "feature/x")
    assert captured_cmds[-1] == ["git", "branch", "-d", "feature/x"]


def test_delete_branch_force_uses_force_delete_flag(monkeypatch, _fake_git_repo) -> None:
    """force=True must use ``-D`` so an unmerged branch can still be discarded."""
    captured_cmds = []
    monkeypatch.setattr(
        git_utils,
        "_run_git",
        lambda path, cmd, *a, **kw: (captured_cmds.append(cmd), (0, ""))[1],
    )
    ok, _ = git_utils.delete_branch(_fake_git_repo, "feature/x", force=True)
    assert ok is True
    assert captured_cmds[-1] == ["git", "branch", "-D", "feature/x"]
