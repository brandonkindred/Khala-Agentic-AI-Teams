# Git-Revision Previous Content Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-open helper that reads file blobs from a caller-supplied git revision via `git show`, returning the same hit/miss result shape as the on-disk leaf.

**Architecture:** Extend `code_review_agent/previous_content.py`: rename the result type to `PreviousContentResult` (keep `PreviousContentDiskResult` as an alias), add `read_previous_content_from_git` that preflights revision resolution then calls `shared.git.git_utils._run_git(..., merge_stderr=False)` per path. No aggregator or review wiring.

**Tech Stack:** Python 3.10+, `shared.git.git_utils._run_git`, pytest (`tmp_path` + real `git init` fixture)

## Global Constraints

- Work only in worktree `.worktrees/5435-resolve-previous-content-git` on branch `5435-resolve-previous-content-git`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new/changed public type and function
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Caller-supplied `revision` required (no default `HEAD` / `HEAD~1` policy in this leaf)
- Blank/whitespace `repo_path` or `revision` → `ValueError`; all other git/environment failures → misses, never abort
- Out of scope: disk behavior changes beyond rename/alias; aggregating disk+git; change-surface / `CodeReviewInput` wiring

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/previous_content.py` | Rename result; alias; add `read_previous_content_from_git` |
| `backend/agents/software_engineering_team/tests/test_previous_content_git.py` | Create: git hit/miss/unavailable/blank tests |
| `backend/agents/software_engineering_team/tests/test_previous_content_ondisk.py` | Unchanged imports (alias keeps `PreviousContentDiskResult` working); run to confirm |
| `backend/shared/git/git_utils.py` | Reuse `_run_git` only; do not modify |
| `backend/agents/software_engineering_team/code_review_agent/repo_reader.py` | Reuse `DEFAULT_MAX_FILE_BYTES` only; do not modify |

Spec: `docs/superpowers/specs/2026-08-07-previous-content-git-design.md`

---

### Task 1: Shared result rename + git previous-content reader

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/previous_content.py`
- Create: `backend/agents/software_engineering_team/tests/test_previous_content_git.py`
- Verify unchanged behavior: `backend/agents/software_engineering_team/tests/test_previous_content_ondisk.py`

**Interfaces:**
- Consumes:
  - `shared.git.git_utils._run_git(repo_path: Path, cmd: list[str], timeout: int = 30, *, merge_stderr: bool = True) -> tuple[int, str]`
  - `code_review_agent.repo_reader.DEFAULT_MAX_FILE_BYTES`
  - Existing `read_previous_content_from_disk` / `DiskRepoReader` (unchanged logic)
- Produces:
  - `PreviousContentResult(contents: dict[str, str], misses: frozenset[str])` (frozen dataclass)
  - `PreviousContentDiskResult = PreviousContentResult`
  - `read_previous_content_from_git(repo_path: str, revision: str, paths: Iterable[str]) -> PreviousContentResult`
  - `read_previous_content_from_disk(...) -> PreviousContentResult` (same behavior; return type uses shared name)

- [ ] **Step 1: Write the failing git tests**

Create `backend/agents/software_engineering_team/tests/test_previous_content_git.py`:

```python
"""Tests for git-revision previous-content resolution (``code_review_agent.previous_content``)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from code_review_agent.previous_content import (
    PreviousContentDiskResult,
    PreviousContentResult,
    read_previous_content_from_git,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _commit_file(repo: Path, rel: str, content: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-m", f"add {rel}")


def test_alias_previous_content_disk_result_is_shared_type() -> None:
    assert PreviousContentDiskResult is PreviousContentResult


def test_git_hit_returns_blob_text(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "old = 1\n")
    result = read_previous_content_from_git(str(repo), "HEAD", ["a.py"])
    assert isinstance(result, PreviousContentResult)
    assert result.contents == {"a.py": "old = 1\n"}
    assert result.misses == frozenset()


def test_git_miss_for_absent_path(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", ["missing.py"])
    assert "missing.py" not in result.contents
    assert result.misses == frozenset({"missing.py"})


def test_fail_open_batch_one_hit_one_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "present.py", "x = 1\n")
    result = read_previous_content_from_git(
        str(repo),
        "HEAD",
        ["present.py", "absent.py"],
    )
    assert result.contents == {"present.py": "x = 1\n"}
    assert result.misses == frozenset({"absent.py"})


def test_no_git_repo_all_misses(tmp_path: Path) -> None:
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    result = read_previous_content_from_git(str(bare), "HEAD", ["a.py", "b.py"])
    assert result.contents == {}
    assert result.misses == frozenset({"a.py", "b.py"})


def test_bad_revision_all_misses(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "no-such-rev-zzzz", ["a.py"])
    assert result.contents == {}
    assert result.misses == frozenset({"a.py"})


def test_blank_revision_raises(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    with pytest.raises(ValueError):
        read_previous_content_from_git(str(repo), "", ["a.py"])
    with pytest.raises(ValueError):
        read_previous_content_from_git(str(repo), "   ", ["a.py"])


def test_blank_repo_path_raises() -> None:
    with pytest.raises(ValueError):
        read_previous_content_from_git("", "HEAD", ["a.py"])


def test_empty_paths_returns_empty_result(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", [])
    assert result.contents == {}
    assert result.misses == frozenset()


def test_unsafe_path_is_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", ["../secret"])
    assert result.contents == {}
    assert result.misses == frozenset({"../secret"})


def test_blank_path_string_is_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _commit_file(repo, "a.py", "x\n")
    result = read_previous_content_from_git(str(repo), "HEAD", [""])
    assert result.contents == {}
    assert result.misses == frozenset({""})
```

- [ ] **Step 2: Run git tests to verify they fail**

From `backend/` (use the worktree’s or linked venv pytest):

```bash
pytest agents/software_engineering_team/tests/test_previous_content_git.py -v
```

Expected: FAIL with `ImportError` / missing `PreviousContentResult` or `read_previous_content_from_git`.

- [ ] **Step 3: Implement rename + git reader**

Replace `backend/agents/software_engineering_team/code_review_agent/previous_content.py` with:

```python
"""Resolve previous file content from on-disk workspace or a git revision.

Disk reads return the literal current bytes under ``repo_path`` (often already
post-execution *new* content). Git reads return blobs at a caller-supplied
revision via ``git show``. Neither path judges trustworthiness or aggregates
sources — callers compose results. Per-path failures are misses; only blank
``repo_path`` / ``revision`` raise.
"""

from __future__ import annotations

import dataclasses
import os
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
        - Per-path: unsafe/blank path, missing blob, non-zero ``git show``,
          or oversize blob → miss; success → hit with stdout text.
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
        rc, out = _run_git(
            root,
            ["git", "show", spec],
            merge_stderr=False,
        )
        if rc != 0:
            misses.add(path)
            continue
        if len(out.encode("utf-8", errors="surrogateescape")) > DEFAULT_MAX_FILE_BYTES:
            misses.add(path)
            continue
        contents[path] = out

    return PreviousContentResult(contents=contents, misses=frozenset(misses))
```

Notes for the implementer:

- Prefer `f"{stripped_rev}^{{commit}}"` so the shell/git sees `rev^{commit}`.
- Do not modify `repo_reader.py` or `shared/git/git_utils.py`.
- Keep disk loop behavior identical; only rename the result type.

- [ ] **Step 4: Run git tests to verify they pass**

```bash
pytest agents/software_engineering_team/tests/test_previous_content_git.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Confirm on-disk suite still passes**

```bash
pytest agents/software_engineering_team/tests/test_previous_content_ondisk.py -v
```

Expected: 7 passed (alias keeps `PreviousContentDiskResult` imports working).

- [ ] **Step 6: Coverage + lint**

```bash
pytest agents/software_engineering_team/tests/test_previous_content_git.py \
  agents/software_engineering_team/tests/test_previous_content_ondisk.py \
  --cov=code_review_agent.previous_content --cov-report=term-missing

ruff check agents/software_engineering_team/code_review_agent/previous_content.py \
  agents/software_engineering_team/tests/test_previous_content_git.py
```

Expected: ≥ 90% line coverage on `previous_content.py`; ruff clean.

- [ ] **Step 7: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/previous_content.py \
  backend/agents/software_engineering_team/tests/test_previous_content_git.py
git commit -m "$(cat <<'EOF'
Add fail-open git-revision previous-content reader for SE review.

Shares PreviousContentResult with the disk leaf and resolves blobs via
git show so callers can fill old_contents from a configured revision.
EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Caller-supplied `revision` | Task 1 |
| `PreviousContentResult` + disk alias | Task 1 |
| `read_previous_content_from_git` + `_run_git` / `merge_stderr=False` | Task 1 |
| Blank repo/revision → `ValueError` | Task 1 |
| Unusable git / bad rev → all misses | Task 1 |
| Per-path miss / path safety / size cap | Task 1 |
| Real mini-repo + fail-open tests | Task 1 |
| No aggregator / wiring | Not in plan |

No placeholders; signatures match the design doc.
