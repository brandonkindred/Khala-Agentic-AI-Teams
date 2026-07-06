"""Whole-repo read access for the false-positive verifier.

The false-positive filter's ``CodebaseIndex`` sees only the *submission* — the
changed files plus a small pre-existing-code excerpt. That blind spot is what
lets a finding like "this module does not exist / must be created" or "this
relative import is unresolved" survive as a false positive when the target
already exists elsewhere in the repository, unchanged and therefore absent from
the diff. A ``RepoReader`` gives the verifier read access to the *rest* of the
repository so it can confirm the file/module already exists and drop the
finding.

Contract for every reader:

    - **Read-only and thread-safe.** Verification fans findings out across a
      ``ThreadPoolExecutor``; a reader is shared across those workers and must
      never mutate observable state without its own synchronization. Concrete
      readers cache lookups under an internal lock.
    - **Fail-safe.** ``read_file`` returns ``None`` (never raises) for an absent,
      unreadable, or out-of-bounds path, and ``list_files`` returns ``[]`` rather
      than raising. A reader failure therefore only ever *keeps* a finding (the
      verifier could not confirm the false positive), never drops a real one.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from typing import List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Cap on the number of paths a reader enumerates, so ``list_files`` on a large
# repository cannot flood the verifier prompt/tool result. Files beyond the cap
# are still readable by exact path via ``read_file`` (which does not depend on
# the listing), so an existence check for a specifically-cited path still works.
DEFAULT_MAX_LISTED_FILES = 5_000

# Cap on a single file's size (bytes) the disk reader will return, so an
# accidentally-huge file cannot blow up a verification prompt. A file over the
# cap reads as ``None`` (treated as "cannot confirm", i.e. the finding is kept).
DEFAULT_MAX_FILE_BYTES = 1_000_000

# Cap on the number of (path -> content) entries the read cache retains, bounding
# resident memory (up to ~max_file_bytes each). Independent of the listing cap:
# the read cache is an LRU over recently-checked paths, not the file inventory.
DEFAULT_MAX_READ_CACHE = 512

# Directories never worth listing/reading for a code existence check.
_SKIP_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
    }
)


@runtime_checkable
class RepoReader(Protocol):
    """Read-only, thread-safe view of a repository beyond the submission.

    Implementations must satisfy the module-level contract (read-only,
    thread-safe, fail-safe). ``@runtime_checkable`` lets callers ``isinstance``
    a duck-typed reader (e.g. one built in coding_team and passed across the
    engine boundary) without a shared base class.
    """

    def list_files(self) -> List[str]:
        """Return repository-relative file paths (bounded, may be empty)."""
        ...

    def read_file(self, path: str) -> Optional[str]:
        """Return the file's text, or ``None`` when absent/unreadable. Never raises."""
        ...


class DiskRepoReader:
    """A ``RepoReader`` backed by a materialized git checkout on disk.

    Used by the software-engineering pipeline, whose reviews run inside a
    per-job workspace (``SE_WORKSPACE_DIR``). Reads are confined under
    ``repo_root`` (a path escaping it via ``..``/symlink resolves outside and is
    refused), bounded in size, and memoized under a lock.

    Invariants:
        - Never mutates the filesystem; every public method is a pure read.
        - ``read_file``/``list_files`` never raise — an OS error degrades to
          ``None``/``[]`` respectively, preserving the verifier's fail-safe rule.
    """

    def __init__(
        self,
        repo_root: str,
        *,
        max_listed_files: int = DEFAULT_MAX_LISTED_FILES,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_read_cache: int = DEFAULT_MAX_READ_CACHE,
    ) -> None:
        """Bind the reader to ``repo_root``.

        Preconditions:
            - ``repo_root`` is a non-empty path string; ``max_listed_files``,
              ``max_file_bytes``, and ``max_read_cache`` are positive.
        """
        assert repo_root and repo_root.strip(), "repo_root must be a non-empty path"
        assert max_listed_files > 0 and max_file_bytes > 0 and max_read_cache > 0, (
            "caps must be positive"
        )
        self._root = os.path.realpath(repo_root)
        self._max_listed = max_listed_files
        self._max_bytes = max_file_bytes
        self._max_read_cache = max_read_cache
        self._lock = threading.Lock()
        self._read_cache: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._listing: Optional[List[str]] = None

    def _resolve_under_root(self, path: str) -> Optional[str]:
        """Resolve ``path`` to an absolute path confined under ``repo_root``.

        Postconditions:
            - Returns the real absolute path when it stays under ``repo_root``;
              returns ``None`` for a blank path or one that escapes the root
              (``..`` traversal, absolute path elsewhere, or a symlink out).
        """
        key = (path or "").strip().lstrip("/")
        if not key:
            return None
        candidate = os.path.realpath(os.path.join(self._root, key))
        if candidate == self._root or candidate.startswith(self._root + os.sep):
            return candidate
        return None

    def read_file(self, path: str) -> Optional[str]:
        """Return the text of ``path`` under the repo root, or ``None``.

        Postconditions:
            - Returns the file's decoded text for a readable file within the size
              cap; ``None`` for a blank/out-of-bounds path, a missing file, a
              directory, an over-cap file, or any OS/decoding error. Never raises.
            - The (path -> result) lookup is memoized under a lock, so repeated
              checks of the same path cost one disk read.
        """
        resolved = self._resolve_under_root(path)
        if resolved is None:
            return None
        with self._lock:
            if resolved in self._read_cache:
                self._read_cache.move_to_end(resolved)
                return self._read_cache[resolved]
        content = self._read_from_disk(resolved)
        with self._lock:
            self._read_cache[resolved] = content
            self._read_cache.move_to_end(resolved)
            while len(self._read_cache) > self._max_read_cache:
                self._read_cache.popitem(last=False)
        return content

    def _read_from_disk(self, resolved: str) -> Optional[str]:
        """Read one already-resolved absolute path, degrading to ``None``.

        Postconditions:
            - Returns the decoded text of a regular file within the size cap,
              else ``None`` (missing, directory, too large, or unreadable).
              Never raises.
        """
        try:
            if not os.path.isfile(resolved):
                return None
            if os.path.getsize(resolved) > self._max_bytes:
                return None
            with open(resolved, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError as exc:
            logger.debug("DiskRepoReader: could not read %s: %s", resolved, exc)
            return None

    def list_files(self) -> List[str]:
        """Return repository-relative paths under the root (bounded, cached).

        Postconditions:
            - Returns up to ``max_listed_files`` repo-relative paths (POSIX
              separators), skipping VCS/build/cache directories, in sorted order.
              Returns ``[]`` on any walk error. Computed once and memoized.
        """
        with self._lock:
            if self._listing is not None:
                return list(self._listing)
        listing = self._walk()
        with self._lock:
            self._listing = listing
        return list(listing)

    def _walk(self) -> List[str]:
        """Walk ``repo_root`` collecting bounded, filtered relative paths.

        Postconditions:
            - Returns sorted repo-relative POSIX paths, skipping ``_SKIP_DIRS``
              and capped at ``max_listed_files``; ``[]`` on any OS error.
        """
        found: List[str] = []
        try:
            for dirpath, dirnames, filenames in os.walk(self._root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for name in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, name), self._root)
                    found.append(rel.replace(os.sep, "/"))
                    if len(found) >= self._max_listed:
                        logger.debug(
                            "DiskRepoReader: listing hit cap %s; truncating", self._max_listed
                        )
                        return sorted(found)
        except OSError as exc:
            logger.debug("DiskRepoReader: walk failed under %s: %s", self._root, exc)
            return []
        return sorted(found)
