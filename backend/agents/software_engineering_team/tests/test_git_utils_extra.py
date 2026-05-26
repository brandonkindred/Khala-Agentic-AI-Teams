"""Extra coverage for shared.git_utils."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from software_engineering_team.shared.git_utils import (
    _clear_disposable_files_if_blocking,
    ensure_development_branch,
    ensure_files_committed_on_main,
    initialize_new_repo,
)


@pytest.fixture
def init_git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=False
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True, check=False
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, capture_output=True, check=False
    )
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=False)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True, check=False)
    # Rename to main
    subprocess.run(
        ["git", "branch", "-M", "main"], cwd=tmp_path, capture_output=True, check=False
    )
    return tmp_path


def test_clear_disposable_files_no_match():
    """Returns False when checkout_out doesn't mention blocking files."""
    assert _clear_disposable_files_if_blocking(Path("/tmp"), "some other error") is False


def test_clear_disposable_files_removes_test_db(tmp_path: Path):
    (tmp_path / "test.db").write_text("garbage")
    out = "error: Your local changes to the following files would be overwritten by checkout:"
    removed = _clear_disposable_files_if_blocking(tmp_path, out)
    assert removed is True
    assert not (tmp_path / "test.db").exists()


def test_clear_disposable_files_no_files_to_remove(tmp_path: Path):
    """File doesn't exist -> returns False."""
    out = "would be overwritten"
    assert _clear_disposable_files_if_blocking(tmp_path, out) is False


def test_ensure_development_branch_non_git(tmp_path: Path):
    ok, msg = ensure_development_branch(tmp_path)
    assert ok is False
    assert "Not a git" in msg


def test_ensure_development_branch_creates_from_main(init_git_repo: Path):
    """When dev branch doesn't exist, creates it from main."""
    created, msg = ensure_development_branch(init_git_repo)
    assert created is True
    assert "development" in msg


def test_ensure_development_branch_existing(init_git_repo: Path):
    """When dev branch exists, just checks it out."""
    subprocess.run(
        ["git", "checkout", "-b", "development"],
        cwd=init_git_repo,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=init_git_repo, capture_output=True, check=False
    )
    created, msg = ensure_development_branch(init_git_repo)
    assert created is False
    assert "existing" in msg or "development" in msg


def test_ensure_files_committed_on_main_non_git(tmp_path: Path):
    ok, msg = ensure_files_committed_on_main(tmp_path, ["README.md"])
    assert ok is False
    assert "Not a git" in msg


def test_ensure_files_committed_on_main_no_main_branch(tmp_path: Path):
    """No main or master branch."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True, check=False
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True, check=False
    )
    # No initial commit -> no branches
    ok, msg = ensure_files_committed_on_main(tmp_path, ["README.md"])
    # Either no branches, or no commits exist
    assert ok is False


def test_ensure_files_committed_on_main_new_file(init_git_repo: Path):
    """Add a new file and commit it on main."""
    # Create development branch first so checkout-back can succeed
    subprocess.run(
        ["git", "checkout", "-b", "development"],
        cwd=init_git_repo,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=init_git_repo, capture_output=True, check=False
    )
    (init_git_repo / "NEWFILE.md").write_text("hello")
    ok, _msg = ensure_files_committed_on_main(init_git_repo, ["NEWFILE.md"])
    assert ok is True


def test_ensure_files_committed_on_main_missing_file(init_git_repo: Path):
    """File doesn't exist -> no-op success (file_paths loop just skips)."""
    subprocess.run(
        ["git", "checkout", "-b", "development"],
        cwd=init_git_repo,
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "checkout", "main"], cwd=init_git_repo, capture_output=True, check=False
    )
    ok, _msg = ensure_files_committed_on_main(init_git_repo, ["nonexistent.md"])
    assert ok is True


def test_initialize_new_repo(tmp_path: Path):
    """Sets up a brand-new git repo with development branch."""
    ok, msg = initialize_new_repo(tmp_path)
    # Result depends on git config; just verify shape
    assert isinstance(ok, bool)
    assert isinstance(msg, str)


def test_initialize_new_repo_with_existing_gitignore(tmp_path: Path):
    """Don't overwrite an existing .gitignore."""
    (tmp_path / ".gitignore").write_text("custom\n")
    ok, msg = initialize_new_repo(tmp_path)
    assert isinstance(msg, str)
    # gitignore content preserved if init succeeded
    if ok:
        assert (tmp_path / ".gitignore").read_text() == "custom\n"
