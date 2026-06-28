"""Shared test helpers for software-engineering team tests."""

from __future__ import annotations

import subprocess
from pathlib import Path


def init_repo_with_existing_development(path: Path) -> None:
    """Create a git repo with main and development branches for setup tests."""
    subprocess.run(["git", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path,
        capture_output=True,
        check=True,
    )
    (path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "-b", "development"], cwd=path, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "main"], cwd=path, capture_output=True, check=True)
