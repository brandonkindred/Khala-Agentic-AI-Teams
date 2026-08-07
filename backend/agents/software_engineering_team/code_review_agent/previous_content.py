"""Resolve previous file content from the on-disk workspace checkout.

Reads the literal current bytes under ``repo_path`` for each requested path.
After gated execution the workspace often already holds *new* content, so a
hit may equal the post-execution text — this module does not judge
trustworthiness. Per-path failures are misses; only a blank ``repo_path``
raises.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, FrozenSet, Iterable, Set

from code_review_agent.repo_reader import DiskRepoReader


@dataclasses.dataclass(frozen=True)
class PreviousContentDiskResult:
    """Partition of on-disk previous-content lookups into hits and misses.

    Invariants:
        - ``contents.keys()`` and ``misses`` are disjoint.
        - Every distinct input path string appears in exactly one of
          ``contents`` or ``misses``.
    """

    contents: Dict[str, str]
    misses: FrozenSet[str]


def read_previous_content_from_disk(
    repo_path: str,
    paths: Iterable[str],
) -> PreviousContentDiskResult:
    """Read literal on-disk text for each path under ``repo_path``.

    Preconditions:
        - ``repo_path`` is a strip-nonempty path string; otherwise raise
          ``ValueError``.
        - ``paths`` is an iterable of strings (may be empty).

    Postconditions:
        - Returns ``PreviousContentDiskResult`` where each unique path string
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

    return PreviousContentDiskResult(contents=contents, misses=frozenset(misses))
