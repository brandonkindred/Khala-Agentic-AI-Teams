"""Cross-process exclusive lock via an advisory ``fcntl.flock`` on a sibling file.

:func:`flock_lock` is the single, tested primitive for the "serialize access to
a shared resource across worker *processes* (not just threads in one process)
via an exclusive lock on a well-known file" pattern this codebase uses in more
than one place — originally hand-rolled independently by ``unified_api``'s
``_ensure_repo_clone`` (serializing concurrent clone/fetch of one per-issue
checkout) and ``software_engineering_team``'s ``pip_install_lock`` (serializing
concurrent same-stack backend workers' shared-interpreter pip install). Both
now build on this module instead of maintaining their own copy of the same
POSIX ``flock`` bookkeeping (open → ``flock(LOCK_EX)`` → critical section →
``flock(LOCK_UN)`` → close), so a fix to one (e.g. a descriptor-leak edge case)
benefits both call sites.

Deliberately minimal: this module does not create the lock file's parent
directory (a caller that needs one must ``mkdir`` it first) and does not
swallow ``OSError`` from ``open``/``flock`` — different callers want different
failure handling (``_ensure_repo_clone`` surfaces it as an error string;
``pip_install_lock`` logs a warning and degrades to running unguarded), so
that decision stays with the caller rather than being baked into this
primitive.
"""

from __future__ import annotations

import contextlib
import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = ["flock_lock"]


@contextmanager
def flock_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive ``fcntl.flock`` on ``path`` for the duration of the ``with`` block.

    Preconditions:
        - ``path``'s parent directory already exists.
    Postconditions:
        - While held, no other caller (thread or process) holding this same
          lock (same resolved ``path``) can be inside its own
          ``with flock_lock(path)`` block at the same time.
        - The lock is released and the underlying file descriptor closed
          before this context manager returns control to the caller,
          including when the ``with`` block's body raises — release and
          close both happen in ``finally`` blocks.
    Raises:
        - ``OSError`` if ``path`` cannot be opened, or if ``flock(LOCK_EX)``
          fails — propagated to the caller rather than swallowed, since
          different callers want different failure handling.
    """
    lock_file = open(path, "w", encoding="utf-8")  # noqa: SIM115 - closed in the finally below
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_file, fcntl.LOCK_UN)
    finally:
        lock_file.close()
