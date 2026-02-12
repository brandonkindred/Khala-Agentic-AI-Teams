"""
Git utilities for the software engineering team branching strategy.

The Tech Lead enforces: all development on a development branch;
create it from main if it does not exist.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

DEVELOPMENT_BRANCH = "development"
MAIN_BRANCH = "main"


def _run_git(repo_path: Path, cmd: list[str], timeout: int = 30) -> Tuple[int, str]:
    """Run git command in repo. Returns (returncode, stdout+stderr)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "Command timed out"
    except Exception as e:
        return -1, str(e)


def create_feature_branch(repo_path: str | Path, base_branch: str, feature_name: str) -> Tuple[bool, str]:
    """
    Create and checkout a feature branch from base_branch.
    feature_name: e.g. "t3-backend-auth" (will become feature/t3-backend-auth).

    If the branch already exists (e.g. from a previous run), it is deleted
    and recreated from the base branch so the task gets a clean start.

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    branch_name = f"feature/{feature_name}" if not feature_name.startswith("feature/") else feature_name
    code, out = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
    if code != 0:
        if "already exists" in out:
            # Stale branch from a previous run — delete and recreate
            logger.warning(
                "Branch '%s' already exists, deleting and recreating from '%s'",
                branch_name, base_branch,
            )
            _run_git(path, ["git", "checkout", base_branch])
            del_code, del_out = _run_git(path, ["git", "branch", "-D", branch_name])
            if del_code != 0:
                return False, f"Failed to delete stale branch {branch_name}: {del_out}"
            code2, out2 = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
            if code2 != 0:
                return False, f"Failed to recreate branch {branch_name}: {out2}"
        else:
            return False, f"Failed to create branch {branch_name}: {out}"
    logger.info("Created branch '%s' from '%s'", branch_name, base_branch)
    return True, branch_name


def checkout_branch(repo_path: str | Path, branch: str) -> Tuple[bool, str]:
    """Checkout the given branch. Returns (success, message)."""
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "checkout", branch])
    if code != 0:
        return False, f"Failed to checkout {branch}: {out}"
    return True, f"Checked out {branch}"


def write_files_and_commit(
    repo_path: str | Path,
    files_dict: Dict[str, str],
    message: str,
) -> Tuple[bool, str]:
    """
    Write files to repo, git add, and commit on the current branch.
    files_dict: { "path/relative/to/repo": "content" }

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    for file_path, content in files_dict.items():
        full_path = path / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
    code, out = _run_git(path, ["git", "add", "-A"])
    if code != 0:
        return False, f"git add failed: {out}"
    code, out = _run_git(path, ["git", "status", "--porcelain"])
    if code != 0:
        return False, f"git status failed: {out}"
    if not out.strip():
        logger.info("No changes to commit (files unchanged)")
        return True, "No changes to commit"
    code, out = _run_git(path, ["git", "commit", "-m", message])
    if code != 0:
        return False, f"git commit failed: {out}"
    logger.info("Committed: %s", message[:50])
    return True, "Committed"


def merge_branch(repo_path: str | Path, source_branch: str, target_branch: str) -> Tuple[bool, str]:
    """
    Checkout target_branch and merge source_branch into it.
    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "checkout", target_branch])
    if code != 0:
        return False, f"Failed to checkout {target_branch}: {out}"
    code, out = _run_git(path, ["git", "merge", source_branch, "-m", f"Merge {source_branch} into {target_branch}"])
    if code != 0:
        return False, f"Merge failed: {out}"
    return True, f"Merged {source_branch} into {target_branch}"


def delete_branch(repo_path: str | Path, branch: str) -> Tuple[bool, str]:
    """Delete the branch (must not be checked out). Returns (success, message)."""
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    code, out = _run_git(path, ["git", "branch", "-d", branch])
    if code != 0:
        return False, f"Failed to delete branch {branch}: {out}"
    return True, f"Deleted branch {branch}"


def ensure_development_branch(repo_path: str | Path) -> Tuple[bool, str]:
    """
    Ensure the development branch exists. Create it from main if it does not.

    Returns:
        (created, message) - created=True if branch was created, message describes action.
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"

    # Check if development branch exists
    code, out = _run_git(path, ["git", "branch", "-a"])
    if code != 0:
        return False, f"git branch failed: {out}"
    branches = [b.strip().lstrip("* ").split("/")[-1] for b in out.splitlines() if b.strip()]
    if DEVELOPMENT_BRANCH in branches:
        code, out = _run_git(path, ["git", "checkout", DEVELOPMENT_BRANCH])
        if code != 0:
            return False, f"Failed to checkout {DEVELOPMENT_BRANCH}: {out}"
        return False, f"Checked out existing branch '{DEVELOPMENT_BRANCH}'"

    # Ensure we have main or master
    if MAIN_BRANCH not in branches and "master" not in branches:
        return False, "Neither 'main' nor 'master' branch found; create an initial commit first"

    base = MAIN_BRANCH if MAIN_BRANCH in branches else "master"
    code, out = _run_git(path, ["git", "checkout", "-b", DEVELOPMENT_BRANCH, base])
    if code != 0:
        return False, f"Failed to create development branch: {out}"
    logger.info("Created branch '%s' from '%s'", DEVELOPMENT_BRANCH, base)
    return True, f"Created branch '{DEVELOPMENT_BRANCH}' from '{base}'"
