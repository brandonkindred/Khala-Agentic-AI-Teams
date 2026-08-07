# On-Disk Previous Content Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-open helper that reads literal on-disk file text under `repo_path` for each requested path and returns hits plus explicit misses without aborting the batch.

**Architecture:** New module `code_review_agent/previous_content.py` wraps `DiskRepoReader`: validate strip-nonempty `repo_path` (raise `ValueError`), dedupe path strings exactly, call `read_file` per path, partition into `PreviousContentDiskResult.contents` / `.misses`. No changes to `change_surface.py` or review wiring.

**Tech Stack:** Python 3.10+, dataclasses, pytest (`tmp_path`), existing `DiskRepoReader`

## Global Constraints

- Work only in worktree `.worktrees/5434-resolve-previous-content-ondisk` on branch `5434-resolve-previous-content-ondisk`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public type and function
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Semantics: literal current disk bytes (no compare-to-new filter); trustworthiness is out of scope
- Per-path failures never raise; blank `repo_path` raises `ValueError`
- Out of scope: git resolution, aggregators, change-surface / `CodeReviewInput` wiring

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/previous_content.py` | Create: `PreviousContentDiskResult` + `read_previous_content_from_disk` |
| `backend/agents/software_engineering_team/tests/test_previous_content_ondisk.py` | Create: unit tests for hit / miss / fail-open batch / blank root / empty paths / dedupe |
| `backend/agents/software_engineering_team/code_review_agent/repo_reader.py` | Reuse only (`DiskRepoReader`); do not modify |

Spec: `docs/superpowers/specs/2026-08-07-previous-content-ondisk-design.md`

---

### Task 1: Disk previous-content reader + unit tests

**Files:**
- Create: `backend/agents/software_engineering_team/code_review_agent/previous_content.py`
- Create: `backend/agents/software_engineering_team/tests/test_previous_content_ondisk.py`
- Reuse (do not modify): `backend/agents/software_engineering_team/code_review_agent/repo_reader.py`

**Interfaces:**
- Consumes: `DiskRepoReader` from `code_review_agent.repo_reader` (`read_file(path) -> Optional[str]`)
- Produces:
  - `PreviousContentDiskResult(contents: dict[str, str], misses: frozenset[str])` (frozen dataclass)
  - `read_previous_content_from_disk(repo_path: str, paths: Iterable[str]) -> PreviousContentDiskResult`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_previous_content_ondisk.py`:

```python
"""Tests for on-disk previous-content resolution (``code_review_agent.previous_content``)."""

from __future__ import annotations

import os

import pytest

from code_review_agent.previous_content import (
    PreviousContentDiskResult,
    read_previous_content_from_disk,
)


def _write(root, rel: str, content: str) -> None:
    path = os.path.join(root, rel)
    parent = os.path.dirname(path)
    if parent and parent != str(root):
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_hit_returns_on_disk_text(tmp_path) -> None:
    _write(tmp_path, "a.py", "old = 1\n")
    result = read_previous_content_from_disk(str(tmp_path), ["a.py"])
    assert isinstance(result, PreviousContentDiskResult)
    assert result.contents == {"a.py": "old = 1\n"}
    assert "a.py" not in result.misses
    assert result.misses == frozenset()


def test_miss_for_absent_path(tmp_path) -> None:
    result = read_previous_content_from_disk(str(tmp_path), ["missing.py"])
    assert "missing.py" not in result.contents
    assert result.misses == frozenset({"missing.py"})


def test_fail_open_batch_one_hit_one_miss(tmp_path) -> None:
    _write(tmp_path, "present.py", "x = 1\n")
    result = read_previous_content_from_disk(
        str(tmp_path),
        ["present.py", "absent.py"],
    )
    assert result.contents == {"present.py": "x = 1\n"}
    assert result.misses == frozenset({"absent.py"})


def test_blank_repo_path_raises() -> None:
    with pytest.raises(ValueError):
        read_previous_content_from_disk("", ["a.py"])
    with pytest.raises(ValueError):
        read_previous_content_from_disk("   ", ["a.py"])


def test_empty_paths_returns_empty_result(tmp_path) -> None:
    result = read_previous_content_from_disk(str(tmp_path), [])
    assert result.contents == {}
    assert result.misses == frozenset()


def test_duplicate_path_strings_read_once(tmp_path) -> None:
    _write(tmp_path, "a.py", "once\n")
    result = read_previous_content_from_disk(str(tmp_path), ["a.py", "a.py"])
    assert result.contents == {"a.py": "once\n"}
    assert result.misses == frozenset()
    assert len(result.contents) + len(result.misses) == 1


def test_blank_path_string_is_miss(tmp_path) -> None:
    result = read_previous_content_from_disk(str(tmp_path), [""])
    assert result.contents == {}
    assert result.misses == frozenset({""})
```

- [ ] **Step 2: Run tests to verify they fail**

Run from `backend/`:

```bash
pytest agents/software_engineering_team/tests/test_previous_content_ondisk.py -v
```

Expected: FAIL with `ModuleNotFoundError` / `ImportError` for `code_review_agent.previous_content` (module not defined yet).

- [ ] **Step 3: Write minimal implementation**

Create `backend/agents/software_engineering_team/code_review_agent/previous_content.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
pytest agents/software_engineering_team/tests/test_previous_content_ondisk.py -v
```

Expected: all tests PASS.

Optional coverage check:

```bash
pytest agents/software_engineering_team/tests/test_previous_content_ondisk.py \
  --cov=agents/software_engineering_team/code_review_agent/previous_content \
  --cov-report=term-missing
```

Expected: ≥ 90% line coverage on `previous_content.py`.

- [ ] **Step 5: Lint**

```bash
cd backend && ruff check agents/software_engineering_team/code_review_agent/previous_content.py \
  agents/software_engineering_team/tests/test_previous_content_ondisk.py
```

Expected: no findings. Fix any ruff issues before committing.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/previous_content.py \
  backend/agents/software_engineering_team/tests/test_previous_content_ondisk.py
git commit -m "$(cat <<'EOF'
Add fail-open on-disk previous-content reader for SE review.

Batch-reads workspace files under repo_path into hits and explicit misses
so change-surface old_contents can be filled without aborting on one path.
EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| `read_previous_content_from_disk` + `PreviousContentDiskResult` | Task 1 |
| Literal disk semantics (Approach A) | Task 1 implementation (no compare-to-new) |
| Fail-open per path; blank `repo_path` → `ValueError` | Task 1 tests + impl |
| Hit / miss / batch / empty paths tests | Task 1 |
| DbC docstrings | Task 1 Step 3 |
| Keep I/O out of `change_surface.py` | Task 1 (new module only) |
| Out of scope: git, aggregator, wiring | Not in plan |

No placeholders; types and signatures match the design doc.
