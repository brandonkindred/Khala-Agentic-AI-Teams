"""
Git utilities for the software engineering team branching strategy.

The Tech Lead enforces: all development on a development branch;
create it from main if it does not exist.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Tuple

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

    If the working tree has uncommitted changes, they are committed on the current
    branch first so checkout can succeed (avoids "would be overwritten by checkout").
    If the branch already exists (e.g. from a previous run), it is deleted
    and recreated from the base branch so the task gets a clean start.

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"
    branch_name = f"feature/{feature_name}" if not feature_name.startswith("feature/") else feature_name

    # Ensure working tree is clean so checkout does not fail with "would be overwritten"
    status_code, status_out = _run_git(path, ["git", "status", "--porcelain"])
    if status_code == 0 and status_out.strip():
        _run_git(path, ["git", "add", "-A"])
        commit_code, commit_out = _run_git(
            path, ["git", "commit", "-m", "chore: save working tree before feature branch"]
        )
        if commit_code != 0 and "nothing to commit" not in (commit_out or ""):
            logger.warning("Could not commit before feature branch: %s", commit_out)
        else:
            logger.info("Committed uncommitted changes before creating feature branch")

    code, out = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
    if code != 0:
        if "would be overwritten" in out or "Your local changes" in out:
            # First try removing disposable files (e.g. test.db) that block checkout
            if _clear_disposable_files_if_blocking(path, out):
                code, out = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
                if code == 0:
                    logger.info("Created branch '%s' from '%s' (disposable files cleared)", branch_name, base_branch)
                    return True, branch_name
            # Working tree still dirty — try stash
            logger.info("Checkout failed due to local changes, trying stash")
            stash_code, stash_out = _run_git(path, ["git", "stash", "push", "-u", "-m", "pre-feature-branch"])
            if stash_code == 0:
                code, out = _run_git(path, ["git", "checkout", "-b", branch_name, base_branch])
                if code == 0:
                    logger.info("Created branch '%s' from '%s' (changes stashed)", branch_name, base_branch)
                    return True, branch_name
            return False, f"Failed to create branch {branch_name}: {out}"
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
        if _clear_disposable_files_if_blocking(path, out):
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


# Default .gitignore for new repos (Python, Node, IDE)
_DEFAULT_GITIGNORE = """# Byte-compiled / optimized / DLL files
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual environments
.venv
venv/
ENV/
env/

# Node
node_modules/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
.npm
.eslintcache

# IDE
.idea/
.vscode/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db
"""


def initialize_new_repo(
    repo_path: str | Path,
    *,
    gitignore_content: str | None = None,
) -> Tuple[bool, str]:
    """
    Initialize a directory as a new git repo: init, .gitignore, README.md, CONTRIBUTORS.md,
    initial commit, rename master to main, create and checkout development branch.

    If the path is already a git repo, ensures development branch exists and checks it out.
    Writes .gitignore, README.md, CONTRIBUTORS.md only if they do not already exist
    (so callers can pre-create them with desired content).

    Args:
        repo_path: Path to the directory to initialize.
        gitignore_content: Optional content for .gitignore. If provided and .gitignore
            does not exist, this is used; otherwise _DEFAULT_GITIGNORE is used.

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    path.mkdir(parents=True, exist_ok=True)

    if (path / ".git").exists():
        ok, msg = ensure_development_branch(path)
        # Idempotent: already a repo; ensuring development is success
        return True, f"Already a git repo; {msg}"

    # 1. git init
    code, out = _run_git(path, ["git", "init"])
    if code != 0:
        return False, f"git init failed: {out}"

    # 2. .gitignore, README.md, CONTRIBUTORS.md (only if missing)
    gitignore_path = path / ".gitignore"
    if not gitignore_path.exists():
        content = gitignore_content if gitignore_content is not None else _DEFAULT_GITIGNORE
        gitignore_path.write_text(content, encoding="utf-8")
    if not (path / "README.md").exists():
        (path / "README.md").write_text("", encoding="utf-8")
    if not (path / "CONTRIBUTORS.md").exists():
        (path / "CONTRIBUTORS.md").write_text("", encoding="utf-8")

    # 3. Initial commit
    code, out = _run_git(path, ["git", "add", "-A"])
    if code != 0:
        return False, f"git add failed: {out}"
    code, out = _run_git(path, ["git", "commit", "-m", "Initial commit"])
    if code != 0:
        return False, f"Initial commit failed: {out}"

    # 4. Rename master to main (git init may create master or main depending on version)
    code, out = _run_git(path, ["git", "branch", "--show-current"])
    current_branch = (out or "").strip() if code == 0 else "master"
    if current_branch == "master":
        code, out = _run_git(path, ["git", "branch", "-m", "master", "main"])
        if code != 0:
            return False, f"Rename master to main failed: {out}"

    # 5. Create development branch and switch to it
    code, out = _run_git(path, ["git", "checkout", "-b", DEVELOPMENT_BRANCH])
    if code != 0:
        return False, f"Create development branch failed: {out}"
    logger.info("Initialized new repo at %s with development branch", path)
    return True, f"Initialized repo at {path}; on branch {DEVELOPMENT_BRANCH}"


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


# Disposable files that can be removed before checkout to avoid "would be overwritten" errors.
# These are typically generated by tests (e.g. SQLite test.db) and should not block branch switches.
_DISPOSABLE_FILES_BEFORE_CHECKOUT = ("test.db", "*.db")


def _clear_disposable_files_if_blocking(path: Path, checkout_out: str) -> bool:
    """
    If checkout failed due to local changes in disposable files (e.g. test.db),
    remove those files so checkout can succeed on retry.
    Returns True if any file was removed.
    """
    removed = False
    if "would be overwritten" not in checkout_out and "Your local changes" not in checkout_out:
        return False
    for name in _DISPOSABLE_FILES_BEFORE_CHECKOUT:
        if "*" in name:
            continue  # Skip glob patterns for now; handle literal test.db
        fp = path / name
        if fp.exists():
            try:
                fp.unlink()
                logger.info("Removed disposable file %s to allow branch checkout", name)
                removed = True
            except OSError as e:
                logger.warning("Could not remove %s before checkout: %s", name, e)
    return removed


def ensure_files_committed_on_main(
    repo_path: str | Path,
    file_paths: List[str],
    *,
    commit_message: str = "Add README, CONTRIBUTORS, .gitignore",
) -> Tuple[bool, str]:
    """
    Ensure the given files are committed on the main branch.
    Checkouts main, adds the files, commits if there are changes, then checkouts development.
    Idempotent: no-op if files are already committed.

    Returns (success, message).
    """
    path = Path(repo_path).resolve()
    if not (path / ".git").exists():
        return False, "Not a git repository"

    # Check if main branch exists
    code, out = _run_git(path, ["git", "branch", "-a"])
    if code != 0:
        return False, f"git branch failed: {out}"
    branches = [b.strip().lstrip("* ").split("/")[-1] for b in out.splitlines() if b.strip()]
    if MAIN_BRANCH not in branches and "master" not in branches:
        return False, "Neither 'main' nor 'master' branch found"

    base = MAIN_BRANCH if MAIN_BRANCH in branches else "master"
    current_branch_code, current_out = _run_git(path, ["git", "branch", "--show-current"])
    current_branch = (current_out or "").strip() if current_branch_code == 0 else ""

    # Checkout main (clear disposable files like test.db if they block)
    code, out = _run_git(path, ["git", "checkout", base])
    if code != 0:
        if _clear_disposable_files_if_blocking(path, out):
            code, out = _run_git(path, ["git", "checkout", base])
        if code != 0:
            return False, f"Failed to checkout {base}: {out}"

    # Add the files
    for fp in file_paths:
        if (path / fp).exists():
            code, out = _run_git(path, ["git", "add", fp])
            if code != 0:
                _run_git(path, ["git", "checkout", current_branch or DEVELOPMENT_BRANCH])
                return False, f"git add {fp} failed: {out}"

    # Check if there are changes to commit
    code, out = _run_git(path, ["git", "status", "--porcelain"])
    if code != 0:
        _run_git(path, ["git", "checkout", current_branch or DEVELOPMENT_BRANCH])
        return False, f"git status failed: {out}"

    if out.strip():
        code, out = _run_git(path, ["git", "commit", "-m", commit_message])
        if code != 0:
            _run_git(path, ["git", "checkout", current_branch or DEVELOPMENT_BRANCH])
            return False, f"git commit failed: {out}"
        logger.info("Committed %s on %s", file_paths, base)

    # Checkout back to development (or original branch)
    target = current_branch if current_branch and current_branch != base else DEVELOPMENT_BRANCH
    code, out = _run_git(path, ["git", "checkout", target])
    if code != 0:
        if _clear_disposable_files_if_blocking(path, out):
            code, out = _run_git(path, ["git", "checkout", target])
        if code != 0:
            return False, f"Failed to checkout {target}: {out}"

    return True, f"Files committed on {base}"
