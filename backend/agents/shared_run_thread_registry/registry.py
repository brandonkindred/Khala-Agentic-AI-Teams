"""RunThreadRegistry — per-job orchestrator/run-thread liveness tracking.

Invariants:
    - ``_threads`` and ``_starting_jobs`` are mutated only under ``_lock``, never rebound, so the
      ``.threads``/``.starting_jobs``/``.lock`` aliases stay live references to the same objects
      the instance methods operate on.
"""

from __future__ import annotations

import threading
from typing import Dict


class RunThreadRegistry:
    """Tracks which thread (if any) is running a job's orchestrator, plus in-flight start claims.

    One instance per team (not a process-wide singleton) — construct one in the team's ``state``
    module and bind thin wrappers over it if the team's call sites need module-level function names
    (e.g. for ``monkeypatch.setattr(module, "name", ...)`` compatibility).
    """

    def __init__(self) -> None:
        self._threads: Dict[str, threading.Thread] = {}
        self._starting_jobs: set[str] = set()
        self._lock = threading.Lock()

    def register(self, job_id: str) -> None:
        """Record the current thread as job_id's owner.

        Preconditions:
            - job_id is non-empty.
        Postconditions:
            - ``is_alive(job_id)`` reflects the current thread's liveness; any pending claim on
              job_id is released.
        """
        with self._lock:
            self._threads[job_id] = threading.current_thread()
            self._starting_jobs.discard(job_id)

    def clear(self, job_id: str) -> None:
        """Drop job_id's owning thread and any pending claim.

        Postconditions:
            - ``is_alive(job_id)`` is False and ``claim(job_id)`` succeeds immediately after.
        """
        with self._lock:
            self._threads.pop(job_id, None)
            self._starting_jobs.discard(job_id)

    def is_alive(self, job_id: str) -> bool:
        """True if a thread is registered for job_id and it is still running."""
        t = self._threads.get(job_id)
        return t is not None and t.is_alive()

    def claim(self, job_id: str) -> bool:
        """Atomically claim the right to start an orchestrator thread for job_id.

        Postconditions:
            - Returns True (and marks job_id 'starting') iff no thread is alive for job_id and no
              claim is already outstanding for it; returns False with no state change otherwise.
              The claim is released by ``register`` (once the new thread registers) or ``clear``.
        """
        with self._lock:
            if (self._threads.get(job_id) is not None and self._threads[job_id].is_alive()) or (
                job_id in self._starting_jobs
            ):
                return False
            self._starting_jobs.add(job_id)
            return True

    @property
    def threads(self) -> Dict[str, threading.Thread]:
        """Direct-poke alias onto the internal thread map; back-compat only, prefer the methods above."""
        return self._threads

    @property
    def starting_jobs(self) -> set:
        """Direct-poke alias onto the internal claim set; back-compat only, prefer the methods above."""
        return self._starting_jobs

    @property
    def lock(self) -> threading.Lock:
        """Alias onto the internal lock; back-compat only."""
        return self._lock
