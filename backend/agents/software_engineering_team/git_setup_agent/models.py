"""Models for the Git Setup agent."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GitSetupResult:
    """Result of running the Git Setup agent."""

    success: bool
    message: str

    def __bool__(self) -> bool:
        return self.success
