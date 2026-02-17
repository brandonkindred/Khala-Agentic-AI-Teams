"""Unit tests for git_utils, including test.db handling and branch checkout."""

import subprocess
from pathlib import Path

import pytest

from shared.git_utils import (
    _clear_disposable_files_if_blocking,
    checkout_branch,
    create_feature_branch,
    ensure_files_committed_on_main,
    ensure_development_branch,
    DEVELOPMENT_BRANCH,
    MAIN_BRANCH,
)


def test_clear_disposable_files_returns_false_when_no_blocking_message() -> None:
    """_clear_disposable_files_if_blocking returns False when output has no blocking message."""
    assert _clear_disposable_files_if_blocking(Path("/tmp"), "some other error") is False


def test_clear_disposable_files_removes_test_db_when_blocking(tmp_path: Path) -> None:
    """When checkout fails due to test.db, _clear_disposable_files_if_blocking removes it."""
    test_db = tmp_path / "test.db"
    test_db.write_bytes(b"sqlite content")
    assert test_db.exists()

    out = "error: Your local changes to the following files would be overwritten by checkout:\n\ttest.db"
    result = _clear_disposable_files_if_blocking(tmp_path, out)

    assert result is True
    assert not test_db.exists()


def test_checkout_branch_clears_test_db_when_blocking(tmp_path: Path) -> None:
    """checkout_branch removes test.db when it blocks checkout."""
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "f"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "other"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "test.db").write_bytes(b"x")
    subprocess.run(["git", "add", "test.db"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add db"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "test.db").write_bytes(b"modified")
    # Initial commit is on master (or main); we created 'other' from it
    result = subprocess.run(["git", "branch", "-a"], cwd=tmp_path, capture_output=True, text=True)
    out = result.stdout or ""
    base = "main" if "main" in (out or "") else "master"
    ok, msg = checkout_branch(tmp_path, base)
    assert ok is True, msg
