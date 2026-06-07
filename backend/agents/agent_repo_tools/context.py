"""Execution context for read-only repo-inspection tools: the workspace root is host-injected."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoToolContext:
    """Binds the inspection tools to a single job workspace.

    The model must never choose ``repo_path``; callers construct this from
    orchestrator state. Every tool path is resolved relative to (and confined
    to) this root.

    Preconditions:
        - ``repo_path`` is a filesystem path to the job workspace.
    Postconditions:
        - ``repo_path`` is stored fully resolved (absolute, symlinks collapsed).
    Invariants:
        - ``repo_path`` is always an absolute, resolved ``Path``.
    """

    repo_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "repo_path", Path(self.repo_path).resolve())
