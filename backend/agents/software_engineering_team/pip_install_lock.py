"""Cross-process lock guarding the coding-team backend's shared pip install.

Every backend build/test call site installs the task's ``requirements.txt``
into the running interpreter's own environment
(``sys.executable -m pip install -r requirements.txt``, no ``--target``/venv
isolation) before invoking ``pytest`` against that same interpreter. That is a
mutation of one process-wide ``site-packages``, not of the caller's per-worker
git worktree (see ``worktree_manager``'s module docstring) — so once two
same-stack backend workers (``backend_v2-1``, ``backend_v2-2``, ...) can run
concurrently, their installs race against each other: interleaved writes to
the same package's files can corrupt the shared environment, or let one
worker's ``pytest`` run start against a half-written package.

:func:`pip_install_lock` serializes that install step, and the ``pytest`` run
that reads its result, across concurrent callers via an exclusive
``fcntl.flock`` on a single well-known lock file. Every call site holds the
lock across *both* the install and the subsequent ``run_pytest`` call, not
just the install: ``pytest`` imports whatever is in ``site-packages`` at
collection time, so releasing the lock between install and pytest would let
a second worker's install mutate those same packages mid-collection —
reintroducing the exact race this lock exists to prevent.

The lock itself is the same mechanism (and the same reasoning: it must hold
"even across worker processes on the shared host volume", not only threads
in one process) that ``unified_api``'s ``_ensure_repo_clone`` already uses to
serialize concurrent clone/fetch of one checkout (see
``clone_workspace.clone_lock_path``). Unlike that per-checkout lock, this one
is *not* keyed by repo/worktree path: the resource it protects (the shared
interpreter's ``site-packages``) is the same regardless of which worktree's
``requirements.txt`` is being installed, so a single fixed path is correct
here.

Trade-off (documented per the module owning the wider guarantee,
``worktree_manager``): this serializes concurrent same-stack backend
workers' install-and-test step — a single global serialization point, not
per-worker isolation. Two same-stack workers with tests can no longer build
their pytest results concurrently; each waits for the other's install+test
window to finish. A per-worktree virtualenv would fully isolate installs
(and let pytest runs overlap) instead, at the cost of a fresh ``pip install``
(disk + time) per worktree and plumbing every downstream
``run_pytest(python_exe=...)`` call to the right interpreter; the lock is
the minimal fix for the actual failure mode (races on the shared
environment), not a general isolation mechanism.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from software_engineering_team.clone_workspace import agent_cache_dir

logger = logging.getLogger(__name__)


def pip_install_lock_path() -> Path:
    """Return the single fixed lock path guarding the shared pip install.

    Postconditions:
        - Returns the same path on every call. Pure: no filesystem access.
        - The path is not derived from any repo/worktree path — the resource
          it protects (the shared interpreter's ``site-packages``) is the
          same regardless of which worktree's ``requirements.txt`` triggered
          the install.
    """
    return Path(agent_cache_dir()) / "coding_team" / ".pip_install.lock"


@contextmanager
def pip_install_lock() -> Iterator[None]:
    """Hold an exclusive, cross-process lock for the duration of the ``with`` block.

    Postconditions:
        - While held, no other caller (thread or process) holding this same
          lock can be inside its own ``with pip_install_lock()`` block at the
          same time.
        - The lock file's parent directory is created if missing.
        - The lock is released (and the file closed) before this context
          manager returns control to the caller, including when the ``with``
          block's body raises — the release happens in a ``finally``.
        - A failure to open or ``flock`` the lock file is logged as a warning
          and swallowed: the wrapped block still runs, unguarded. Every
          caller already treats a pip-install failure as non-fatal, so a
          lock-acquisition failure must not newly make the caller fail
          closed.
    """
    lock_path = pip_install_lock_path()
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w", encoding="utf-8")  # noqa: SIM115 - closed in finally below
    except OSError as e:
        logger.warning("Could not open pip install lock at %s: %s", lock_path, e)
        yield
        return
    try:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        except OSError as e:
            logger.warning("Could not acquire pip install lock at %s: %s", lock_path, e)
            yield
            return
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
