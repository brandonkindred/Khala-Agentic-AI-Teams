"""
Git utilities for the software engineering team branching strategy.

The Tech Lead enforces: all development on a development branch;
create it from main if it does not exist.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)

DEVELOPMENT_BRANCH = "development"
MAIN_BRANCH = "main"


def ensure_development_branch(repo_path: str | Path) -> Tuple[bool, str]:
    """
    Ensure the development branch exists. Create it from main if it does not.

    Returns:
        (created, message) - created=True if branch was created, message describes action.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"

    def _run(cmd: list[str]) -> Tuple[int, str]:
        try:
            result = subprocess.run(
                cmd,
                cwd=path,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return result.returncode, (result.stdout or "") + (result.stderr or "")
        except subprocess.TimeoutExpired:
            return -1, "Command timed out"
        except Exception as e:
            return -1, str(e)

    # Check if development branch exists
    code, out = _run(["git", "branch", "-a"])
    if code != 0:
        return False, f"git branch failed: {out}"
    branches = [b.strip().lstrip("* ").split("/")[-1] for b in out.splitlines() if b.strip()]
    if DEVELOPMENT_BRANCH in branches:
        return False, f"Branch '{DEVELOPMENT_BRANCH}' already exists"

    # Ensure we have main or master
    if MAIN_BRANCH not in branches and "master" not in branches:
        return False, "Neither 'main' nor 'master' branch found; create an initial commit first"

    base = MAIN_BRANCH if MAIN_BRANCH in branches else "master"
    code, out = _run(["git", "checkout", "-b", DEVELOPMENT_BRANCH, base])
    if code != 0:
        return False, f"Failed to create development branch: {out}"
    logger.info("Created branch '%s' from '%s'", DEVELOPMENT_BRANCH, base)
    return True, f"Created branch '{DEVELOPMENT_BRANCH}' from '{base}'"
