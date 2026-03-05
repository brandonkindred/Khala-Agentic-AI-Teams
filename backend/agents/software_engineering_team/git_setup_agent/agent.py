"""Git Setup agent: initializes a directory as a new git repository."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Union

from software_engineering_team.shared.git_utils import initialize_new_repo

from .models import GitSetupResult

logger = logging.getLogger(__name__)


class GitSetupAgent:
    """
    Agent that initializes a path as a new git repo: .gitignore, README.md,
    CONTRIBUTORS.md, initial commit, main branch, development branch.
    No LLM required; purely deterministic.
    """

    def run(self, path: Union[str, Path]) -> GitSetupResult:
        """
        Initialize the given path as a git repository (or ensure development branch if already a repo).

        Steps: git init, add .gitignore + empty README.md + CONTRIBUTORS.md,
        initial commit, rename master to main, create and checkout development.
        """
        path = Path(path).resolve()
        logger.info("Git Setup: initializing repo at %s", path)
        success, message = initialize_new_repo(path)
        if success:
            logger.info("Git Setup: %s", message)
        else:
            logger.warning("Git Setup failed at %s: %s", path, message)
        return GitSetupResult(success=success, message=message)
