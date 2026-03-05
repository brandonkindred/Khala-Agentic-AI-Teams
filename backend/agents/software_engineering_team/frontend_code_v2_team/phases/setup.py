"""
Setup phase: ensure repo exists, README, main branch, development branch.

Runs as the first phase of the Frontend Tech Lead Agent.
Uses shared.git_utils only. No frontend_team code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from software_engineering_team.shared.git_utils import (
    DEVELOPMENT_BRANCH,
    ensure_development_branch,
    initialize_new_repo,
)

from ..models import SetupResult

logger = logging.getLogger(__name__)


def run_setup(
    *,
    repo_path: Path,
    task_title: str = "",
) -> SetupResult:
    """
    Ensure the repository is initialized and ready for frontend development.

    - If the path is not a git repo: git init, create README.md with project title,
      initial commit, rename master to main if needed, create development branch.
    - If already a repo: ensure development branch exists and is checked out;
      optionally ensure README exists (create minimal one if missing).
    """
    result = SetupResult()
    path = Path(repo_path).resolve()

    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)

    if not (path / ".git").exists():
        ok, msg = initialize_new_repo(path)
        if not ok:
            result.summary = f"Setup failed: {msg}"
            logger.error("Setup: %s", result.summary)
            return result
        result.repo_initialized = True
        result.master_renamed_to_main = True
        result.branch_created = True
        result.readme_created = True
        if task_title:
            _ensure_readme_with_title(path, task_title)
        result.summary = f"Initialized repo: {msg}"
        logger.info("Setup: %s", result.summary)
        return result

    ok, msg = ensure_development_branch(path)
    if not ok:
        result.summary = f"Setup failed: {msg}"
        logger.error("Setup: %s", result.summary)
        return result
    if "Created branch" in msg:
        result.branch_created = True
    if not (path / "README.md").exists() and task_title:
        _ensure_readme_with_title(path, task_title)
        result.readme_created = True
    result.summary = msg or "Repo ready; on development branch."
    logger.info("Setup: %s", result.summary)
    return result


def _ensure_readme_with_title(path: Path, title: str) -> None:
    """Write or prepend project title to README.md and commit if possible."""
    readme = path / "README.md"
    content = f"# {title}\n\n"
    if readme.exists():
        existing = readme.read_text(encoding="utf-8")
        if existing.strip() and not existing.lstrip().startswith("#"):
            content = content + existing
        else:
            content = content + existing.lstrip()
    readme.write_text(content, encoding="utf-8")
    try:
        from software_engineering_team.shared.git_utils import write_files_and_commit
        write_files_and_commit(path, {"README.md": content}, "docs: add README with project title")
    except Exception as e:
        logger.warning("Could not commit README: %s", e)
