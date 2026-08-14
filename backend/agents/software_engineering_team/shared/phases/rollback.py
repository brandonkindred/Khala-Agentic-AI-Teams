"""Disk snapshot/rollback helpers for the gated execution loop in ``execution.py``.

The gated per-microtask review loop (``run_gated_execution_impl``) guards every
write to the worktree so a rejected fix, an exhausted retry budget, or a tripped
grounding circuit breaker can be undone without leaving partial output committed.
This module holds that snapshot/restore machinery — capturing a file's pre-write
state before a guarded write and reverting to it on rollback — factored out of
``execution.py`` so it can be read and tested independently of the review loop
that drives it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from software_engineering_team.shared.repo_writer import UnsafeRepoPathError, resolve_safe_repo_path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DiskEntry:
    """Pre-write state of a *physical* worktree path (its ``realpath``), keyed so all
    aliases of one file share a single entry.

    Byte-oriented (never decodes), so a pre-existing binary or non-UTF-8 file is
    snapshotted and restored losslessly instead of raising on read:

    - ``file_bytes`` set: a regular file with those bytes → rollback rewrites them.
    - ``absent``: nothing existed → rollback removes the file the write created.
    - both default: a directory or special file was there → rollback leaves it.
    """

    file_bytes: Optional[bytes] = None
    absent: bool = False


def _resolve_physical_path_in_repo(root: Path, full_path: Path) -> Optional[Path]:
    """Resolve ``full_path`` to the real file a write touches, or ``None`` if it escapes.

    A text write follows symlinks, so the physical file it creates/clobbers is at
    ``os.path.realpath`` — which collapses every alias of one file (a direct path, a
    symlink, or a whole symlink chain, even a dangling one) to a single identity.
    Keying the rollback manifest by this realpath is what makes an alias share the
    one earliest snapshot instead of re-snapshotting already-failed bytes.

    Limitation:
        ``realpath`` does not coalesce *hard links* (two directory entries for one
        inode resolve to two distinct paths), so writing one physical file through
        two hard-linked names within a single microtask is not deduplicated. This is
        left unsupported deliberately: git stores no hard links, so a freshly-cloned
        worktree cannot contain them, making this unreachable in the pipeline.

    Postconditions:
        Returns the fully-resolved path when it is strictly inside ``root``; ``None``
        when it resolves to ``root`` itself or escapes it (a write through a link that
        points outside the repo is a pre-existing ``write_repo_text_files`` concern
        this rollback does not reach outside the repo to undo).
    """
    real = Path(os.path.realpath(full_path))
    if real == root or root not in real.parents:
        return None
    return real


def _file_lock_keys(repo_path: Path, rel_paths: Iterable[str]) -> List[str]:
    """Map microtask-relative paths to physical lock keys (``realpath`` strings).

    ``shared.py`` and ``./shared.py`` (and symlink aliases) collapse to one key
    so concurrent writers serialize on the file they actually touch, not the
    spelling they used. An unsafe or out-of-repo path falls back to the raw
    key so it still serializes against other writers using the same spelling.

    Preconditions:
        ``repo_path`` is the worktree root.
    Postconditions:
        Returns deduplicated keys in first-seen order. An empty ``rel_paths``
        yields an empty list (a no-op for :meth:`KeyedLockManager.lock`).
    """
    root = Path(repo_path).resolve()
    keys: List[str] = []
    seen: set[str] = set()
    for rel_path in rel_paths:
        key = rel_path
        try:
            full_path = resolve_safe_repo_path(root, rel_path)
            real_path = _resolve_physical_path_in_repo(root, full_path)
            if real_path is not None:
                key = str(real_path)
        except UnsafeRepoPathError:
            pass
        if key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _snapshot_disk_state(real_path: Path) -> _DiskEntry:
    """Capture the pre-write state of physical ``real_path`` as a :class:`_DiskEntry`.

    Preconditions:
        ``real_path`` is a physical (``realpath``-resolved) path inside the worktree.
    Postconditions:
        Returns ``file_bytes`` for a regular file, ``absent`` when nothing exists,
        or an empty (leave-alone) entry for a directory/special file. A read/stat
        ``OSError`` (e.g. an unreadable file) is logged and degraded to a leave-alone
        entry rather than raised, so rollback bookkeeping never aborts the run.
    Note:
        A regular file's bytes are read whole into memory and held on the microtask's
        rollback manifest until it completes or rolls back. The cost is bounded per
        microtask and by file size; avoiding the in-memory copy would require staging
        a temp-file backup, deliberately not done here to keep the mechanism simple
        (a code-gen microtask overwrites small text files, not large binaries).
    """
    try:
        if real_path.is_file():
            return _DiskEntry(file_bytes=real_path.read_bytes())
        if real_path.exists():
            # A directory or special file — not something the text writer creates.
            return _DiskEntry()
        return _DiskEntry(absent=True)
    except OSError as exc:
        logger.warning("Rollback snapshot skipped for %s: %s", real_path, exc)
        return _DiskEntry()


def _restore_disk_state(real_path: Path, entry: _DiskEntry) -> None:
    """Revert physical ``real_path`` to the state captured in ``entry``.

    Preconditions:
        ``entry`` was produced by :func:`_snapshot_disk_state` for ``real_path``.
    Postconditions:
        Restores the file's prior bytes when ``entry.file_bytes`` is set (recreating
        parent directories as needed), removes the path when ``entry.absent`` (the
        microtask created it), or leaves it untouched for a directory/special or
        degraded leave-alone entry.
    """
    if entry.file_bytes is not None:
        real_path.parent.mkdir(parents=True, exist_ok=True)
        real_path.write_bytes(entry.file_bytes)
    elif entry.absent:
        real_path.unlink(missing_ok=True)
    # else: a pre-existing directory/special file — leave it as-is.


@dataclass
class _MicrotaskRollback:
    """Per-microtask undo manifest for both the in-memory result and the worktree.

    The result and the worktree key their state differently and revert to different
    baselines, so each gets its own snapshot:

    - ``all_files_prior`` is keyed by the *raw* microtask key (the
      ``ExecutionResult.files`` key space) and holds the value that was in
      ``all_files`` before this microtask first wrote that key (``None`` when the key
      was absent → remove on rollback). Sourcing from ``all_files`` keeps a
      pre-existing repo file — which was never an execution output — out of the
      result, and removes every raw key the microtask added, including two spellings
      of one path.
    - ``disk_prior`` is keyed by the *physical* worktree path (``realpath``), so every
      alias of one file — a lexical spelling (``a.py`` / ``/a.py`` / ``./a.py``), a
      symlink, or a symlink chain — collapses to a single entry. It holds the
      pre-write filesystem state (:class:`_DiskEntry`) so a rollback restores a
      pre-existing file's exact bytes (binary-safe) and removes only what the
      microtask actually created.

    Invariants:
        Each raw key is recorded in ``all_files_prior`` at most once, and each physical
        path in ``disk_prior`` at most once — the earliest snapshot, so a later fix
        cycle (even one writing the file through a different alias) never overwrites
        the pre-microtask baseline.
    """

    all_files_prior: Dict[str, Optional[str]] = field(default_factory=dict)
    disk_prior: Dict[Path, _DiskEntry] = field(default_factory=dict)


def _record_prior_values(
    rollback: _MicrotaskRollback,
    repo_path: Path,
    all_files: Dict[str, str],
    files: Dict[str, str],
) -> None:
    """Snapshot, for each key this microtask is about to write, the state to restore.

    Records two baselines per key (see :class:`_MicrotaskRollback`): the prior
    ``all_files`` value under the raw key, and the prior on-disk state under the
    physical (``realpath``) path. Each is recorded at most once (the earliest snapshot
    wins), so a later fix cycle that rewrites the same file — even through a different
    alias that resolves to the same physical path — never overwrites the baseline.

    Preconditions:
        Called *before* writing ``files`` to the worktree (and before
        ``all_files.update(files)``), so both reads reflect the pre-write state.
        ``repo_path`` is the worktree root.
    Postconditions:
        For each key of ``files`` not already recorded: ``rollback.all_files_prior``
        gains ``key → all_files.get(key)`` and ``rollback.disk_prior`` gains
        ``realpath → _DiskEntry`` (the pre-write filesystem state) unless that physical
        path is already recorded. An unsafe key (traversal/empty) or a path that
        resolves outside the repo is skipped for ``disk_prior``. A key already recorded
        on an earlier write is skipped entirely (its baseline must not be overwritten).
    """
    root = Path(repo_path).resolve()
    for rel_path in files:
        if rel_path in rollback.all_files_prior:
            # Already snapshotted on an earlier write; skip re-resolving. A fixed key's
            # physical path does not change mid-microtask, so this would be wasted work.
            continue
        rollback.all_files_prior[rel_path] = all_files.get(rel_path)
        try:
            full_path = resolve_safe_repo_path(root, rel_path)
        except UnsafeRepoPathError:
            continue
        real_path = _resolve_physical_path_in_repo(root, full_path)
        if real_path is not None and real_path not in rollback.disk_prior:
            rollback.disk_prior[real_path] = _snapshot_disk_state(real_path)


def _rollback_microtask_files(
    rollback: _MicrotaskRollback, all_files: Dict[str, str], mt: Any
) -> None:
    """Undo a microtask's contributions to ``all_files``, the worktree, AND its record.

    The commit that follows execution stages the worktree with ``git add -A``, so an
    in-memory-only rollback is not enough: files this microtask left on disk
    (newly-created ones, and overwrites of an earlier microtask's or a pre-existing
    repo file) would still be committed. Reverting both keeps ``all_files`` and the
    worktree — hence the committed tree — consistent with a microtask that never ran.

    Preconditions:
        ``rollback`` was populated by :func:`_record_prior_values` before each of this
        microtask's writes; ``mt`` is the microtask being rolled back.
    Postconditions:
        ``all_files``: each recorded raw key is restored to its prior value, or removed
        when that prior value is ``None`` (the key was absent before). Worktree: each
        recorded physical path is reverted to its pre-write state (:class:`_DiskEntry`)
        — prior bytes restored or a created file removed; a symlink the write followed
        is never touched (only its physical target is). ``mt.output_files`` is cleared,
        so a rolled-back microtask reports no surviving output. Untouched paths unchanged.
    """
    for key, prior in rollback.all_files_prior.items():
        if prior is None:
            all_files.pop(key, None)
        else:
            all_files[key] = prior
    for full_path, entry in rollback.disk_prior.items():
        _restore_disk_state(full_path, entry)
    # A rolled-back microtask produced nothing that survives; clear its record so a
    # consumer of ``mt.output_files`` cannot resurrect the reverted files.
    mt.output_files = {}
