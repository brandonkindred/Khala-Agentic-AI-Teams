"""Resolve previous file content from on-disk workspace or a git revision.

Disk reads return the literal current bytes under ``repo_path`` (often already
post-execution *new* content). Git reads return blobs at a caller-supplied
revision: each path is typed/sized with ``git cat-file`` (``-t`` / ``-s``)
before ``git show`` so oversize or non-blob objects are never loaded as text.

``merge_previous_content`` is a pure preferred/fallback merge for callers that
already hold two partitions (for example a known pre-write disk snapshot).
``resolve_previous_content`` is blank-revision → disk-only, or non-blank →
git-only: it does **not** disk-fill git misses, because workspace bytes after
execution are usually the *new* surface and would collapse diffs (old == new).
Per-path failures are misses; only blank ``repo_path`` raises on disk reads,
and blank ``repo_path`` / ``revision`` raise on leaf git reads (blank revision
on ``resolve_previous_content`` is disk-only).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Set

from code_review_agent.repo_reader import DEFAULT_MAX_FILE_BYTES, DiskRepoReader

from shared.git.git_utils import _run_git

# Cap per-call git blob fetches (type + size + show spawns each). Paths beyond
# the cap are misses so a huge path list cannot fan out into unbounded subprocesses.
_MAX_GIT_BLOBS_READ = 50


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
        - If the path is not a usable git repo, ``revision`` starts with ``-``
          (flag-like), or ``revision`` does not resolve to a commit, every
          unique path is a miss (no raise).
        - Per-path: unsafe/blank path, missing/non-blob object (tree/dir),
          non-zero ``git cat-file`` / ``git show``, oversize blob (size via
          ``git cat-file -s`` before ``git show``), or binary (NUL) blob → miss;
          success → hit with UTF-8-safe stdout text.
        - At most ``_MAX_GIT_BLOBS_READ`` unique paths are fetched; any additional
          unique paths are misses (spawn bound).
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
    # Leading ``-`` would be parsed as a git option by ``rev-parse --verify``;
    # ``--`` after ``--verify`` is not portable (breaks normal HEAD peels on
    # common git builds), so reject flag-like revisions as all-miss.
    if stripped_rev.startswith("-"):
        return _all_misses(unique)
    verify_rc, _ = _run_git(
        root,
        ["git", "rev-parse", "--verify", f"{stripped_rev}^{{commit}}"],
        merge_stderr=True,
    )
    if verify_rc != 0:
        return _all_misses(unique)

    to_fetch = unique[:_MAX_GIT_BLOBS_READ]
    overflow = unique[_MAX_GIT_BLOBS_READ:]

    contents: Dict[str, str] = {}
    misses: Set[str] = set(overflow)
    for path in to_fetch:
        normalized = _normalize_git_path(path)
        if normalized is None:
            misses.add(path)
            continue
        spec = f"{stripped_rev}:{normalized}"
        # Require a blob: trees/directories would otherwise make ``git show``
        # succeed with a pretty-printed listing and become a false hit.
        type_rc, type_out = _run_git(
            root,
            ["git", "cat-file", "-t", spec],
            merge_stderr=False,
        )
        if type_rc != 0 or type_out.strip() != "blob":
            misses.add(path)
            continue
        # Size before show so an oversize blob is never loaded whole.
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
        if "\x00" in out:
            misses.add(path)
            continue
        # Match merge-base reader: UTF-8/JSON-safe text from surrogateescape stdout.
        contents[path] = out.encode("utf-8", "surrogateescape").decode("utf-8", "replace")

    return PreviousContentResult(contents=contents, misses=frozenset(misses))


def merge_previous_content(
    preferred: PreviousContentResult,
    fallback: PreviousContentResult,
) -> PreviousContentResult:
    """Merge two previous-content partitions, preferring ``preferred`` hits.

    Preconditions:
        - ``preferred`` and ``fallback`` are ``PreviousContentResult`` values
          (may be empty).

    Postconditions:
        - Hits start as ``preferred.contents``; each path in
          ``fallback.contents`` not already preferred is taken from fallback.
        - Path universe is the union of both results' ``contents`` keys and
          ``misses``; final ``misses`` are universe minus final hit keys.
        - Preferred wins on overlap. Pure: no I/O; never raises for empty or
          partial inputs.
    """
    contents: Dict[str, str] = dict(preferred.contents)
    for path, text in fallback.contents.items():
        if path not in contents:
            contents[path] = text
    universe: Set[str] = set(preferred.contents)
    universe.update(preferred.misses)
    universe.update(fallback.contents)
    universe.update(fallback.misses)
    misses = frozenset(universe - contents.keys())
    return PreviousContentResult(contents=contents, misses=misses)


def resolve_previous_content(
    repo_path: str,
    paths: Iterable[str],
    revision: str | None = None,
) -> PreviousContentResult:
    """Resolve previous content: disk-only without revision, else git-only.

    Preconditions:
        - ``repo_path`` is strip-nonempty; otherwise raise ``ValueError``.
        - ``paths`` is an iterable of strings (may be empty).
        - ``revision`` may be ``None``/blank (disk-only) or strip-nonempty
          (git-only).

    Postconditions:
        - Empty ``paths`` → empty ``contents`` and empty ``misses``.
        - Blank/missing ``revision`` → ``read_previous_content_from_disk``.
        - Non-blank ``revision`` → ``read_previous_content_from_git`` only
          (no disk fill). Git misses stay misses — including untracked paths
          present on disk and unusable revisions (flag-like, unresolved, no
          ``.git``) — so post-execution workspace bytes are never treated as
          previous content. Callers that intentionally merge a safe disk
          snapshot with git should use ``merge_previous_content``.
        - Never raises for leaf/environment failures once ``repo_path`` is valid.
    """
    stripped_repo = (repo_path or "").strip()
    if not stripped_repo:
        raise ValueError("repo_path must be a non-empty path")

    unique = _unique_paths(paths)
    if not unique:
        return PreviousContentResult(contents={}, misses=frozenset())

    stripped_rev = (revision or "").strip()
    if not stripped_rev:
        return read_previous_content_from_disk(stripped_repo, unique)

    return read_previous_content_from_git(stripped_repo, stripped_rev, unique)
