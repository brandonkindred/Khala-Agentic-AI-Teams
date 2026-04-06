"""
Per-team token bucket rate limiter for LLM calls.

Prevents one team's batch job from starving other teams of LLM capacity.
Each team gets a configurable token bucket; calls block (with timeout)
when the bucket is empty.

Usage::

    from llm_service.rate_limiter import acquire_llm_slot, RateLimitExceeded

    try:
        acquire_llm_slot("blogging", timeout=30.0)
        result = llm.complete_json(prompt)
    finally:
        release_llm_slot("blogging")

Configuration via environment variables:

    LLM_RATE_LIMIT_PER_TEAM=8       Max concurrent LLM calls per team (default: 8)
    LLM_RATE_LIMIT_GLOBAL=16        Max concurrent LLM calls across all teams (default: 16)
    LLM_RATE_LIMIT_TIMEOUT=60       Seconds to wait before giving up (default: 60)
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class RateLimitExceeded(Exception):
    """Raised when a team exceeds its LLM rate limit and the timeout expires."""

    def __init__(self, team: str, timeout: float):
        super().__init__(
            f"LLM rate limit exceeded for team '{team}' (waited {timeout}s)"
        )
        self.team = team
        self.timeout = timeout


class TeamRateLimiter:
    """Per-team and global concurrency limiter for LLM calls.

    Uses bounded semaphores to limit concurrent calls per team and globally.
    """

    def __init__(
        self,
        per_team_limit: int = 8,
        global_limit: int = 16,
        default_timeout: float = 60.0,
    ) -> None:
        self.per_team_limit = per_team_limit
        self.global_limit = global_limit
        self.default_timeout = default_timeout
        self._team_semaphores: Dict[str, threading.BoundedSemaphore] = {}
        self._global_semaphore = threading.BoundedSemaphore(global_limit)
        self._lock = threading.Lock()

    def _get_team_semaphore(self, team: str) -> threading.BoundedSemaphore:
        if team not in self._team_semaphores:
            with self._lock:
                if team not in self._team_semaphores:
                    self._team_semaphores[team] = threading.BoundedSemaphore(
                        self.per_team_limit
                    )
        return self._team_semaphores[team]

    def acquire(self, team: str, timeout: Optional[float] = None) -> bool:
        """Acquire a slot for the given team. Blocks up to timeout seconds.

        Returns True if acquired, raises RateLimitExceeded if timeout expires.
        """
        t = timeout if timeout is not None else self.default_timeout

        # First acquire the per-team slot
        team_sem = self._get_team_semaphore(team)
        if not team_sem.acquire(timeout=t):
            raise RateLimitExceeded(team, t)

        # Then acquire the global slot
        if not self._global_semaphore.acquire(timeout=t):
            team_sem.release()  # Release team slot since we couldn't get global
            raise RateLimitExceeded(team, t)

        return True

    def release(self, team: str) -> None:
        """Release a slot for the given team."""
        try:
            self._global_semaphore.release()
        except ValueError:
            pass
        try:
            sem = self._team_semaphores.get(team)
            if sem:
                sem.release()
        except ValueError:
            pass


# Module-level singleton, configured from environment.
_limiter: Optional[TeamRateLimiter] = None
_limiter_lock = threading.Lock()


def _get_limiter() -> TeamRateLimiter:
    """Return the global rate limiter singleton."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                per_team = int(os.environ.get("LLM_RATE_LIMIT_PER_TEAM", "8"))
                global_limit = int(os.environ.get("LLM_RATE_LIMIT_GLOBAL", "16"))
                timeout = float(os.environ.get("LLM_RATE_LIMIT_TIMEOUT", "60"))
                _limiter = TeamRateLimiter(
                    per_team_limit=per_team,
                    global_limit=global_limit,
                    default_timeout=timeout,
                )
                logger.info(
                    "LLM rate limiter: per_team=%d, global=%d, timeout=%.0fs",
                    per_team,
                    global_limit,
                    timeout,
                )
    return _limiter


def acquire_llm_slot(team: str, timeout: Optional[float] = None) -> None:
    """Acquire an LLM call slot for the given team. Blocks until available or timeout."""
    _get_limiter().acquire(team, timeout)


def release_llm_slot(team: str) -> None:
    """Release an LLM call slot for the given team."""
    _get_limiter().release(team)
