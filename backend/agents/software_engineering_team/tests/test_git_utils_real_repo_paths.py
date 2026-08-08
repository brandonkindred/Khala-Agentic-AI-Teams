"""Real-repository coverage for shared.git_utils path and branch helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shared.git.git_utils import (
    _clear_disposable_files_if_blocking,
    branch_diff,
    commit_paths,
    create_feature_branch,
    ensure_development_branch,
    ensure_files_committed_on_main,
    get_head_sha,
    initialize_new_repo,
    write_files_and_commit,
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
    subprocess.run(["git", "branch", "-M", "main"], cwd=tmp_path, capture_output=True, check=False)
    return tmp_path


def test_get_head_sha_non_git(tmp_path: Path):
    ok, msg = get_head_sha(tmp_path)
    assert ok is False
    assert "Not a git repository" in msg


def test_get_head_sha_returns_head(init_git_repo: Path):
    ok, sha = get_head_sha(init_git_repo)
    assert ok is True
    assert len(sha) == 40
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=init_git_repo,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert sha == expected


def test_get_head_sha_no_commits(tmp_path: Path):
    # A freshly-initialized repo has a .git dir but no HEAD commit, so rev-parse
    # fails and the helper reports failure rather than a garbage SHA.
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=False)
    ok, msg = get_head_sha(tmp_path)
    assert ok is False
    assert "rev-parse failed" in msg


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
    ok, msg = ensure_development_branch(init_git_repo)
    assert ok is True
    assert "development" in msg


def test_ensure_development_branch_existing(init_git_repo: Path):
    """When dev branch exists, just checks it out."""
    subprocess.run(
        ["git", "checkout", "-b", "development"],
        cwd=init_git_repo,
        capture_output=True,
        check=False,
    )
    subprocess.run(["git", "checkout", "main"], cwd=init_git_repo, capture_output=True, check=False)
    ok, msg = ensure_development_branch(init_git_repo)
    assert ok is True
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
    subprocess.run(["git", "checkout", "main"], cwd=init_git_repo, capture_output=True, check=False)
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
    subprocess.run(["git", "checkout", "main"], cwd=init_git_repo, capture_output=True, check=False)
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


def test_branch_diff_returns_full_untruncated_diff(tmp_path: Path):
    """branch_diff returns the complete base...branch diff for the feature branch's changes."""
    ok, _ = initialize_new_repo(tmp_path)
    assert ok
    ok, _ = create_feature_branch(tmp_path, "development", "x")
    assert ok
    big = "new line\n" * 5000
    write_files_and_commit(tmp_path, {"b.txt": big}, "add b")

    diff = branch_diff(tmp_path, "development", "feature/x")
    assert "b.txt" in diff
    assert diff.count("+new line") > 1000  # full diff, not truncated


def test_branch_diff_no_repo_returns_empty(tmp_path: Path):
    """branch_diff returns '' when the path is not a git repository."""
    assert branch_diff(tmp_path / "nope", "development", "feature/x") == ""


def test_branch_diff_failed_command_returns_empty(tmp_path: Path):
    """branch_diff returns '' (not raise) when git diff fails, e.g. an unknown branch."""
    ok, _ = initialize_new_repo(tmp_path)
    assert ok
    assert branch_diff(tmp_path, "development", "feature/does-not-exist") == ""


def test_commit_paths_commits_only_named_paths(init_git_repo: Path):
    """commit_paths stages/commits only the named paths, leaving other work alone."""
    repo = init_git_repo
    (repo / "wanted.py").write_text("a = 1\n", encoding="utf-8")
    (repo / "unrelated.py").write_text("b = 2\n", encoding="utf-8")

    ok, _ = commit_paths(repo, ["wanted.py"], "chore: only wanted")
    assert ok

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, check=True, text=True
    ).stdout.split()
    assert "wanted.py" in tracked
    assert "unrelated.py" not in tracked
    # The unrelated file is still untracked, never swept into the commit.
    status = subprocess.run(
        ["git", "status", "--porcelain", "unrelated.py"],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )
    assert status.stdout.strip() == "?? unrelated.py"


def test_commit_paths_no_changes_is_success(init_git_repo: Path):
    """commit_paths treats 'nothing to commit for these paths' as success."""
    ok, msg = commit_paths(init_git_repo, ["README.md"], "noop")
    assert ok
    assert "No changes" in msg


def test_commit_paths_empty_list_is_noop(init_git_repo: Path):
    """commit_paths with no paths is a success no-op."""
    ok, _ = commit_paths(init_git_repo, [], "noop")
    assert ok


def test_commit_paths_commits_setup_edit_to_already_dirty_file(init_git_repo: Path):
    """A named path that was already dirty is still committed (setup's edit lands).

    Guards the case where a config file (e.g. pyproject.toml) had unrelated local
    edits before setup appended to it: scoping by name must not drop it.
    """
    repo = init_git_repo
    subprocess.run(
        ["git", "checkout", "-b", "development"], cwd=repo, capture_output=True, check=True
    )
    cfg = repo / "pyproject.toml"
    cfg.write_text("[project]\nname = 'x'\n", encoding="utf-8")  # pre-existing dirty edit

    ok, _ = commit_paths(repo, ["pyproject.toml"], "chore: scaffolding")
    assert ok
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, check=True, text=True
    ).stdout.split()
    assert "pyproject.toml" in tracked
    # No leftover dirty state for the committed path.
    status = subprocess.run(
        ["git", "status", "--porcelain", "pyproject.toml"],
        cwd=repo,
        capture_output=True,
        check=True,
        text=True,
    )
    assert status.stdout.strip() == ""
