# Git Previous Content Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-open helper that reads file text from a caller-supplied git revision under `repo_path` (via `git show`) and returns hits plus explicit misses without aborting the batch when git is unavailable.

**Architecture:** Extend `code_review_agent/previous_content.py`: rename the shared result type to `PreviousContentResult` (keep `PreviousContentDiskResult` as an alias), add `read_previous_content_from_git` that validates strip-nonempty `repo_path`/`revision`, preflights the repo+revision, then per unique path runs `git show <rev>:<path>` via `shared.git.git_utils._run_git(..., merge_stderr=False)`. No aggregator or review wiring.

**Tech Stack:** Python 3.10+, dataclasses, pytest (`tmp_path` + real `git init` fixture), `shared.git.git_utils._run_git`, `DEFAULT_MAX_FILE_BYTES` from `repo_reader`

## Global Constraints

- Work only in worktree `.worktrees/5435-resolve-previous-content-git` on branch `5435-resolve-previous-content-git`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new or renamed public type and function
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Revision is required (caller-supplied); no default `HEAD` / `HEAD~1` policy
- Fail-open once preconditions hold: never raise for missing blobs, bad revisions, missing `.git`, timeouts, or other git/environment failures
- Blank/whitespace `repo_path` or `revision` → `ValueError`
- Size cap: inherit `DEFAULT_MAX_FILE_BYTES` (1_000_000); oversize → miss
- Out of scope: on-disk reader behavior changes beyond shared type rename/alias; aggregating disk + git; change-surface / `CodeReviewInput` / `v2_review` wiring; choosing which revision the pipeline should pass

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/previous_content.py` | Modify: rename result → `PreviousContentResult`, alias, add `read_previous_content_from_git` |
| `backend/agents/software_engineering_team/tests/test_previous_content_git.py` | Create: unit tests (real mini-repo hit + fail-open / blank / bad rev) |
| `backend/agents/software_engineering_team/tests/test_previous_content_ondisk.py` | Touch only if alias/isinstance assertions need updating (prefer leave as-is; alias keeps imports working) |
| `backend/shared/git/git_utils.py` | Reuse `_run_git` only; do not modify |
| `backend/agents/software_engineering_team/code_review_agent/repo_reader.py` | Reuse `DEFAULT_MAX_FILE_BYTES` only; do not modify |

Spec: `docs/superpowers/specs/2026-08-07-previous-content-git-design.md`

---

### Task 1: Git previous-content reader + unit tests

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/previous_content.py`
- Create: `backend/agents/software_engineering_team/tests/test_previous_content_git.py`
- Reuse (do not modify): `backend/shared/git/git_utils.py`, `backend/agents/software_engineering_team/code_review_agent/repo_reader.py`

**Interfaces:**
- Consumes:
  - `shared.git.git_utils._run_git(repo_path: Path, cmd: list[str], timeout: int = 30, *, merge_stderr: bool = True) -> tuple[int, str]`
  - `DEFAULT_MAX_FILE_BYTES` from `code_review_agent.repo_reader`
  - Existing `read_previous_content_from_disk` (unchanged behavior)
- Produces:
  - `PreviousContentResult(contents: dict[str, str], misses: frozenset[str])` (frozen dataclass)
  - `PreviousContentDiskResult = PreviousContentResult` (alias)
  - `read_previous_content_from_git(repo_path: str, revision: str, paths: Iterable[str]) -> PreviousContentResult`

- [ ] **Step 1: Write the failing tests**

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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_repo_with_file(root: Path, rel: str, content: str) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "t@t.com")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")


def test_alias_is_previous_content_result() -> None:
    assert PreviousContentDiskResult is PreviousContentResult


def test_hit_returns_blob_at_revision(tmp_path: Path) -> None:
    _init_repo_with_file(tmp_path, "a.py", "old = 1\n")
    result = read_previous_content_from_git(str(tmp_path), "HEAD", ["a.py"])
    assert isinstance(result, PreviousContentResult)
    assert result.contents == {"a.py": "old = 1\n"}
    assert result.misses == frozenset()


def test_miss_for_path_absent_at_revision(tmp_path: Path) -> None:
    _init_repo_with_file(tmp_path, "a.py", "x\n")
    result = read_previous_content_from_git(str(tmp_path), "HEAD", ["missing.py"])
    assert result.contents == {}
    assert result.misses == frozenset({"missing.py"})


def test_unavailable_repo_all_misses_no_raise(tmp_path: Path) -> None:
    # No git init — plain directory.
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    result = read_previous_content_from_git(str(tmp_path), "HEAD", ["a.py"])
    assert result.contents == {}
    assert result.misses == frozenset({"a.py"})


def test_bad_revision_all_misses_no_raise(tmp_path: Path) -> None:
    _init_repo_with_file(tmp_path, "a.py", "x\n")
    result = read_previous_content_from_git(
        str(tmp_path),
        "definitely-not-a-real-revision",
        ["a.py"],
    )
    assert result.contents == {}
    assert result.misses == frozenset({"a.py"})


def test_blank_revision_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_previous_content_from_git(str(tmp_path), "", ["a.py"])
    with pytest.raises(ValueError):
        read_previous_content_from_git(str(tmp_path), "   ", ["a.py"])


def test_blank_repo_path_raises() -> None:
    with pytest.raises(ValueError):
        read_previous_content_from_git("", "HEAD", ["a.py"])
    with pytest.raises(ValueError):
        read_previous_content_from_git("   ", "HEAD", ["a.py"])


def test_fail_open_batch_one_hit_one_miss(tmp_path: Path) -> None:
    _init_repo_with_file(tmp_path, "present.py", "x = 1\n")
    result = read_previous_content_from_git(
        str(tmp_path),
        "HEAD",
        ["present.py", "absent.py"],
    )
    assert result.contents == {"present.py": "x = 1\n"}
    assert result.misses == frozenset({"absent.py"})


def test_empty_paths_returns_empty_result(tmp_path: Path) -> None:
    _init_repo_with_file(tmp_path, "a.py", "x\n")
    result = read_previous_content_from_git(str(tmp_path), "HEAD", [])
    assert result.contents == {}
    assert result.misses == frozenset()


def test_duplicate_path_strings_read_once(tmp_path: Path) -> None:
    _init_repo_with_file(tmp_path, "a.py", "once\n")
    result = read_previous_content_from_git(str(tmp_path), "HEAD", ["a.py", "a.py"])
    assert result.contents == {"a.py": "once\n"}
    assert result.misses == frozenset()
    assert len(result.contents) + len(result.misses) == 1


def test_blank_and_unsafe_paths_are_misses(tmp_path: Path) -> None:
    _init_repo_with_file(tmp_path, "a.py", "x\n")
    result = read_previous_content_from_git(
        str(tmp_path),
        "HEAD",
        ["", "../escape.py", "ok/../../evil.py"],
    )
    assert result.contents == {}
    assert result.misses == frozenset({"", "../escape.py", "ok/../../evil.py"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
pytest agents/software_engineering_team/tests/test_previous_content_git.py -v
```

Expected: FAIL with `ImportError` for `PreviousContentResult` / `read_previous_content_from_git` (not defined yet), or `AttributeError` on missing symbols.

- [ ] **Step 3: Write minimal implementation**

Update `backend/agents/software_engineering_team/code_review_agent/previous_content.py` to the following shape (preserve disk reader behavior; extend module docstring):

```python
"""Resolve previous file content from on-disk workspace and git revisions.

Disk reads return literal current bytes under ``repo_path`` (often post-execution
*new* content). Git reads return blobs at a caller-supplied revision via
``git show``. Per-path failures are misses; only blank ``repo_path`` /
``revision`` (git path) raise.
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


def _normalize_git_path(path: str) -> Optional[str]:
    """Return a repo-relative path safe to pass to ``git show``, or ``None``.

    Postconditions:
        - Blank / whitespace-only → ``None``.
        - Leading ``/`` stripped.
        - Any ``..`` path segment → ``None`` (do not pass escape-like specs to git).
    """
    key = (path or "").strip().lstrip("/")
    if not key:
        return None
    parts = key.split("/")
    if any(part == ".." for part in parts):
        return None
    return key


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


def read_previous_content_from_git(
    repo_path: str,
    revision: str,
    paths: Iterable[str],
) -> PreviousContentResult:
    """Read blob text at ``revision`` for each path under ``repo_path``.

    Preconditions:
        - ``repo_path`` is strip-nonempty; otherwise raise ``ValueError``.
        - ``revision`` is strip-nonempty; otherwise raise ``ValueError``.
        - ``paths`` is an iterable of strings (may be empty).

    Postconditions:
        - Returns ``PreviousContentResult`` partitioning unique path strings into
          hits (``git show <revision>:<path>`` text within ``DEFAULT_MAX_FILE_BYTES``)
          and misses.
        - Duplicate identical path strings are read once.
        - Empty ``paths`` yields empty ``contents`` and empty ``misses``.
        - Once preconditions hold, never raises for missing blobs, bad revisions,
          missing ``.git``, timeouts, or other git/environment failures — those
          degrade to misses (all unique paths on repo/revision preflight failure).
    """
    stripped_repo = (repo_path or "").strip()
    if not stripped_repo:
        raise ValueError("repo_path must be a non-empty path")
    stripped_rev = (revision or "").strip()
    if not stripped_rev:
        raise ValueError("revision must be a non-empty string")

    unique: List[str] = []
    seen: Set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)

    if not unique:
        return PreviousContentResult(contents={}, misses=frozenset())

    root = Path(stripped_repo)
    # Preflight: usable work tree + resolvable commit-ish.
    if not (root / ".git").exists():
        return PreviousContentResult(contents={}, misses=frozenset(unique))
    code, _ = _run_git(
        root,
        ["git", "rev-parse", "--verify", f"{stripped_rev}^{{commit}}"],
        merge_stderr=False,
    )
    if code != 0:
        return PreviousContentResult(contents={}, misses=frozenset(unique))

    contents: Dict[str, str] = {}
    misses: Set[str] = set()
    for path in unique:
        norm = _normalize_git_path(path)
        if norm is None:
            misses.add(path)
            continue
        # Size first (mirrors shared.git read_paths_at_merge_base) so huge blobs
        # are never loaded whole.
        sz_code, sz_out = _run_git(
            root,
            ["git", "cat-file", "-s", f"{stripped_rev}:{norm}"],
            merge_stderr=False,
        )
        if sz_code != 0:
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
        show_code, show_out = _run_git(
            root,
            ["git", "show", f"{stripped_rev}:{norm}"],
            merge_stderr=False,
        )
        if show_code != 0:
            misses.add(path)
            continue
        # Match merge-base reader: UTF-8/JSON-safe text from surrogateescape stdout.
        text = show_out.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        contents[path] = text

    return PreviousContentResult(contents=contents, misses=frozenset(misses))
```

Notes for the implementer:

- Keep result keys as the **original** path strings from the input (not only the normalized form), matching the disk leaf.
- Do not import or call the disk reader from the git function (no aggregation).
- Prefer `root / ".git"` existence plus `rev-parse --verify <rev>^{commit}` for preflight; both failing → all unique paths miss.

- [ ] **Step 4: Run new git tests and existing disk tests**

```bash
pytest agents/software_engineering_team/tests/test_previous_content_git.py \
       agents/software_engineering_team/tests/test_previous_content_ondisk.py -v
```

Expected: all PASS. Disk tests still pass because `PreviousContentDiskResult` is an alias of `PreviousContentResult` and `isinstance` / constructors remain compatible.

- [ ] **Step 5: Lint the touched files**

```bash
ruff check agents/software_engineering_team/code_review_agent/previous_content.py \
           agents/software_engineering_team/tests/test_previous_content_git.py
ruff format agents/software_engineering_team/code_review_agent/previous_content.py \
            agents/software_engineering_team/tests/test_previous_content_git.py
```

Expected: clean / formatted.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/previous_content.py \
  backend/agents/software_engineering_team/tests/test_previous_content_git.py
git commit -m "$(cat <<'EOF'
Add fail-open git-revision previous-content reader for SE review.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `read_previous_content_from_git(repo_path, revision, paths)` | Task 1 |
| `PreviousContentResult` + `PreviousContentDiskResult` alias | Task 1 |
| Required non-blank `revision`; blank → `ValueError` | Task 1 |
| `_run_git(..., merge_stderr=False)` for `git show` | Task 1 |
| Fail-open: unavailable git / bad revision → all misses | Task 1 |
| Per-path miss; batch continues | Task 1 |
| Path safety (`..`, blank) → miss | Task 1 |
| `DEFAULT_MAX_FILE_BYTES` oversize → miss | Task 1 (via `cat-file -s`) |
| Unit tests: hit, miss, unavailable, bad rev, blank inputs | Task 1 |
| No disk behavior change beyond rename/alias | Task 1 |
| No aggregator / CodeReviewInput wiring | (out of scope — no task) |
