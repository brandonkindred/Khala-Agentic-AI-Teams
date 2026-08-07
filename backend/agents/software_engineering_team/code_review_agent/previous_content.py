"""Resolve previous file content from on-disk workspace or a git revision.

Disk reads return the literal current bytes under ``repo_path`` (often already
post-execution *new* content). Git reads return blobs at a caller-supplied
revision via ``git show``. Neither path judges trustworthiness or aggregates
sources — callers compose results. Per-path failures are misses; only blank
``repo_path`` / ``revision`` raise.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set

from code_review_agent.repo_reader import DEFAULT_MAX_FILE_BYTES, DiskRepoReader

from shared.git.git_utils import _run_git


@dataclasses.dataclass(frozen=True)
class PreviousContentResult:
    """Partition of previous-content lookups into hits and misses.

    Invariants:
        - ``contents.keys()`` and ``misses`` are disjoint.
        - Every distinct input path string appears in exactly one of
          ``contents`` or ``misses``.
    """

    contents: Dict[str, str]
    misses: FrozenSet[str]


PreviousContentDiskResult = PreviousContentResult


def read_previous_content_from_disk(
    repo_path: str,
    paths: Iterable[str],
) -> PreviousContentResult:
    """Read literal on-disk text for each path under ``repo_path``.

    Preconditions:
        - ``repo_path`` is a strip-nonempty path string; otherwise raise
          ``ValueError``.
        - ``paths`` is an iterable of strings (may be empty).

    Postconditions:
        - Returns ``PreviousContentResult`` where each unique path string
          is either a hit in ``contents`` (``DiskRepoReader.read_file`` text)
          or a member of ``misses`` (reader returned ``None``).
        - Duplicate identical path strings are read once.
        - Never raises for missing files, path escape, directories, oversize
          files, or ``OSError`` on individual paths.
        - Empty ``paths`` yields empty ``contents`` and empty ``misses``.
    """
    stripped = (repo_path or "").strip()
    if not stripped:
        raise ValueError("repo_path must be a non-empty path")

    reader = DiskRepoReader(stripped)
    contents: Dict[str, str] = {}
    misses: Set[str] = set()
    seen: Set[str] = set()

    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        text = reader.read_file(path)
        if text is None:
            misses.add(path)
        else:
            contents[path] = text

    return PreviousContentResult(contents=contents, misses=frozenset(misses))


def _unique_paths(paths: Iterable[str]) -> List[str]:
    """Return first-seen unique path strings."""
    seen: Set[str] = set()
    out: List[str] = []
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out


def _normalize_git_path(path: str) -> Optional[str]:
    """Return a repo-relative path safe for ``git show rev:path``, or None.

    Postconditions:
        - Returns ``None`` for blank paths, paths with ``..`` segments, or
          absolute-looking segments after strip/lstrip of ``/``.
        - Otherwise returns the stripped path without a leading ``/``.
    """
    key = (path or "").strip().lstrip("/")
    if not key:
        return None
    parts = key.replace("\\", "/").split("/")
    if any(part == ".." or part == "" for part in parts):
        return None
    return "/".join(parts)


def _all_misses(paths: List[str]) -> PreviousContentResult:
    return PreviousContentResult(contents={}, misses=frozenset(paths))


def read_previous_content_from_git(
    repo_path: str,
    revision: str,
    paths: Iterable[str],
) -> PreviousContentResult:
    """Read file blobs at ``revision`` under ``repo_path`` via ``git show``.

    Preconditions:
        - ``repo_path`` is strip-nonempty; otherwise raise ``ValueError``.
        - ``revision`` is strip-nonempty; otherwise raise ``ValueError``.
        - ``paths`` is an iterable of strings (may be empty).

    Postconditions:
        - Returns ``PreviousContentResult`` for the unique path strings.
        - If the path is not a usable git repo or ``revision`` does not
          resolve to a commit, every unique path is a miss (no raise).
        - Per-path: unsafe/blank path, missing blob, non-zero ``git cat-file`` /
          ``git show``, or oversize blob (checked via ``git cat-file -s`` before
          ``git show``) → miss; success → hit with UTF-8-safe stdout text.
        - Duplicate identical path strings are read once.
        - Empty ``paths`` yields empty ``contents`` and empty ``misses``.
        - Never raises for git/environment failures once preconditions hold.
    """
    stripped_repo = (repo_path or "").strip()
    if not stripped_repo:
        raise ValueError("repo_path must be a non-empty path")
    stripped_rev = (revision or "").strip()
    if not stripped_rev:
        raise ValueError("revision must be a non-empty string")

    unique = _unique_paths(paths)
    if not unique:
        return PreviousContentResult(contents={}, misses=frozenset())

    root = Path(stripped_repo)
    # Preflight: usable .git and resolvable commit.
    if not (root / ".git").exists():
        return _all_misses(unique)
    verify_rc, _ = _run_git(
        root,
        ["git", "rev-parse", "--verify", f"{stripped_rev}^{{commit}}"],
        merge_stderr=True,
    )
    if verify_rc != 0:
        return _all_misses(unique)

    contents: Dict[str, str] = {}
    misses: Set[str] = set()
    for path in unique:
        normalized = _normalize_git_path(path)
        if normalized is None:
            misses.add(path)
            continue
        spec = f"{stripped_rev}:{normalized}"
        sz_rc, sz_out = _run_git(
            root,
            ["git", "cat-file", "-s", spec],
            merge_stderr=False,
        )
        if sz_rc != 0:
            misses.add(path)
            continue
        try:
            blob_size = int(sz_out.strip())
        except ValueError:
            misses.add(path)
            continue
        if blob_size > DEFAULT_MAX_FILE_BYTES:
            misses.add(path)
            continue
        rc, out = _run_git(
            root,
            ["git", "show", spec],
            merge_stderr=False,
        )
        if rc != 0:
            misses.add(path)
            continue
        contents[path] = out.encode("utf-8", "surrogateescape").decode("utf-8", "replace")

    return PreviousContentResult(contents=contents, misses=frozenset(misses))
