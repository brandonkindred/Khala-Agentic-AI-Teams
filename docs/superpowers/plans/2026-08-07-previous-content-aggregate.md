# Previous-Content Aggregate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a fail-open aggregator that merges git-preferred and disk-fallback previous-content results into one `PreviousContentResult` for change-surface callers.

**Architecture:** Extend `code_review_agent/previous_content.py` with pure `merge_previous_content(preferred, fallback)` plus thin `resolve_previous_content(repo_path, paths, revision=None)` that runs git-first (disk only for git misses) or disk-only when revision is blank/missing. Reuse `PreviousContentResult` and existing leaf readers; no change-surface wiring.

**Tech Stack:** Python 3.10+, dataclasses, pytest (`tmp_path` + real `git init` for orchestrator mixed case)

## Global Constraints

- Work only in worktree `.worktrees/5436-aggregate-previous-content` on branch `5436-aggregate-previous-content`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:` / `Invariants:` where relevant) on every new public function
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Priority: git-first; blank/missing `revision` → disk-only (do not raise)
- Blank `repo_path` → `ValueError`; partial misses never abort once `repo_path` is valid
- Disk I/O only for git misses (or all paths in disk-only mode)
- Out of scope: changing disk/git leaf behavior; `CodeReviewInput` / `v2_review` / change-surface wiring

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/previous_content.py` | Modify: add `merge_previous_content` + `resolve_previous_content` |
| `backend/agents/software_engineering_team/tests/test_previous_content_aggregate.py` | Create: pure merge + orchestrator unit tests |
| Existing disk/git leaves + their tests | Reuse only; do not modify unless a tiny import alias is required |

Spec: `docs/superpowers/specs/2026-08-07-previous-content-aggregate-design.md`

---

### Task 1: Merge + resolve previous-content aggregator

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/previous_content.py`
- Create: `backend/agents/software_engineering_team/tests/test_previous_content_aggregate.py`

**Interfaces:**
- Consumes:
  - `PreviousContentResult`
  - `read_previous_content_from_disk(repo_path: str, paths: Iterable[str]) -> PreviousContentResult`
  - `read_previous_content_from_git(repo_path: str, revision: str, paths: Iterable[str]) -> PreviousContentResult`
- Produces:
  - `merge_previous_content(preferred: PreviousContentResult, fallback: PreviousContentResult) -> PreviousContentResult`
  - `resolve_previous_content(repo_path: str, paths: Iterable[str], revision: str | None = None) -> PreviousContentResult`

- [ ] **Step 1: Write the failing tests**

Create `backend/agents/software_engineering_team/tests/test_previous_content_aggregate.py`:

```python
"""Tests for aggregating disk/git previous-content results."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from code_review_agent.previous_content import (
    PreviousContentResult,
    merge_previous_content,
    resolve_previous_content,
)


def _result(contents: dict[str, str], misses: set[str]) -> PreviousContentResult:
    return PreviousContentResult(contents=contents, misses=frozenset(misses))


def test_merge_preferred_wins_on_overlap() -> None:
    preferred = _result({"a.py": "from-git\n"}, set())
    fallback = _result({"a.py": "from-disk\n"}, set())
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {"a.py": "from-git\n"}
    assert out.misses == frozenset()


def test_merge_fallback_fills_preferred_miss() -> None:
    preferred = _result({}, {"a.py"})
    fallback = _result({"a.py": "from-disk\n"}, set())
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {"a.py": "from-disk\n"}
    assert out.misses == frozenset()


def test_merge_both_miss_stays_miss() -> None:
    preferred = _result({}, {"a.py"})
    fallback = _result({}, {"a.py"})
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {}
    assert out.misses == frozenset({"a.py"})


def test_merge_empty_preferred_takes_fallback_hits() -> None:
    preferred = _result({}, set())
    fallback = _result({"b.py": "disk\n"}, set())
    out = merge_previous_content(preferred, fallback)
    assert out.contents == {"b.py": "disk\n"}
    assert out.misses == frozenset()


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


def test_resolve_blank_revision_is_disk_only(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("disk\n", encoding="utf-8")
    for rev in (None, "", "   "):
        out = resolve_previous_content(str(root), ["a.py"], revision=rev)
        assert out.contents == {"a.py": "disk\n"}
        assert out.misses == frozenset()


def test_resolve_git_first_mixed_hit_miss(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    tracked = repo / "tracked.py"
    tracked.write_text("old-git\n", encoding="utf-8")
    _git(repo, "add", "tracked.py")
    _git(repo, "commit", "-m", "add tracked")
    # Untracked file: git miss, disk hit.
    (repo / "untracked.py").write_text("only-disk\n", encoding="utf-8")
    out = resolve_previous_content(
        str(repo),
        ["tracked.py", "untracked.py", "absent.py"],
        revision="HEAD",
    )
    assert out.contents["tracked.py"] == "old-git\n"
    assert out.contents["untracked.py"] == "only-disk\n"
    assert out.misses == frozenset({"absent.py"})


def test_resolve_both_miss_no_raise(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    (repo / "README").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-m", "init")
    out = resolve_previous_content(str(repo), ["missing.py"], revision="HEAD")
    assert out.contents == {}
    assert out.misses == frozenset({"missing.py"})


def test_resolve_blank_repo_path_raises() -> None:
    with pytest.raises(ValueError):
        resolve_previous_content("", ["a.py"], revision=None)
    with pytest.raises(ValueError):
        resolve_previous_content("   ", ["a.py"], revision="HEAD")


def test_resolve_empty_paths(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    out = resolve_previous_content(str(root), [], revision=None)
    assert out.contents == {}
    assert out.misses == frozenset()
```

- [ ] **Step 2: Run tests to verify they fail**

From `backend/`:

```bash
pytest agents/software_engineering_team/tests/test_previous_content_aggregate.py -v
```

Expected: FAIL with `ImportError` / missing `merge_previous_content` / `resolve_previous_content`.

- [ ] **Step 3: Write minimal implementation**

Append to `backend/agents/software_engineering_team/code_review_agent/previous_content.py` (keep existing disk/git functions unchanged). Update the module docstring first line area to mention aggregation:

```python
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
    """Resolve previous content git-first, falling back to disk for misses.

    Preconditions:
        - ``repo_path`` is strip-nonempty; otherwise raise ``ValueError``.
        - ``paths`` is an iterable of strings (may be empty).
        - ``revision`` may be ``None``/blank (disk-only) or strip-nonempty
          (git-first).

    Postconditions:
        - Empty ``paths`` → empty ``contents`` and empty ``misses``.
        - Blank/missing ``revision`` → ``read_previous_content_from_disk``.
        - Non-blank ``revision`` → git for all paths; disk only for git
          misses; merge with git preferred. If git has no misses, return git.
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

    git = read_previous_content_from_git(stripped_repo, stripped_rev, unique)
    if not git.misses:
        return git
    disk = read_previous_content_from_disk(stripped_repo, git.misses)
    return merge_previous_content(git, disk)
```

Also extend the top-level module docstring to note aggregation lives here.

If `Optional` is not already imported for annotations, use `str | None` (Python 3.10+) as shown; no new typing imports required beyond what the file already has (`Dict`, `Set`, `Iterable`, `frozenset` usage).

- [ ] **Step 4: Run aggregate + leaf regression tests**

```bash
pytest agents/software_engineering_team/tests/test_previous_content_aggregate.py \
       agents/software_engineering_team/tests/test_previous_content_git.py \
       agents/software_engineering_team/tests/test_previous_content_ondisk.py -v
```

Expected: all PASS.

- [ ] **Step 5: Lint touched files**

```bash
ruff check agents/software_engineering_team/code_review_agent/previous_content.py \
           agents/software_engineering_team/tests/test_previous_content_aggregate.py
ruff format agents/software_engineering_team/code_review_agent/previous_content.py \
            agents/software_engineering_team/tests/test_previous_content_aggregate.py
```

Expected: clean / formatted.

- [ ] **Step 6: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/previous_content.py \
  backend/agents/software_engineering_team/tests/test_previous_content_aggregate.py
git commit -m "$(cat <<'EOF'
Add git-first previous-content aggregator for SE review.

EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `merge_previous_content` preferred-wins + fallback fill | Task 1 |
| Path universe / final misses | Task 1 |
| `resolve_previous_content` blank revision → disk-only | Task 1 |
| Git-first; disk only for git misses | Task 1 |
| Blank `repo_path` → `ValueError` | Task 1 |
| Partial misses do not abort | Task 1 |
| Unit tests: merge + mixed orchestrator | Task 1 |
| No leaf behavior change / no CodeReviewInput wiring | (out of scope — no task) |
