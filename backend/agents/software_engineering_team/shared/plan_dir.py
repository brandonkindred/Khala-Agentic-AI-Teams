"""
Plan folder helper: resolves plan_dir and ensures it exists.

All planning agents write artifacts under the plan folder at project root.
The plan folder is created only after the initial spec is ingested successfully.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PLAN_FOLDER_NAME = "plan"


def ensure_plan_dir(repo_path: str | Path) -> Path:
    """
    Resolve plan_dir from repo_path as {repo_path}/plan and ensure it exists.

    Creates the directory if it does not exist. Use this after the spec is
    successfully ingested, before any planning agent writes output.

    Args:
        repo_path: The work path (project root) that contains initial_spec.md.

    Returns:
        Path to the plan directory (e.g. {repo_path}/plan).
    """
    path = Path(repo_path).resolve()
    plan_dir = path / PLAN_FOLDER_NAME
    plan_dir.mkdir(parents=True, exist_ok=True)
    logger.debug("Plan directory ensured at %s", plan_dir)
    return plan_dir


def get_plan_dir(repo_path: str | Path) -> Path:
    """
    Resolve plan_dir from repo_path without creating it.

    Use when you need the path but creation is handled elsewhere
    (e.g. by ensure_plan_dir called earlier in the flow).

    Args:
        repo_path: The work path (project root).

    Returns:
        Path to the plan directory.
    """
    return Path(repo_path).resolve() / PLAN_FOLDER_NAME
