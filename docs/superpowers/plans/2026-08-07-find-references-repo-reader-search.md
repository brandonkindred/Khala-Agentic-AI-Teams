# find_references Repo Reader Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `CodebaseIndex.find_references` so a present `repo_reader` contributes capped `path:line` hits beyond the submission.

**Architecture:** Keep submission hits from `search` first; fill remaining `max_matches` via a module-private `_search_repo_references` helper in `false_positive_filter.py` that mirrors `side_effect_impact_pass._search_repository` (skip submission paths, match cap, Disk vs non-Disk file-scan defaults, fail-safe). Do not share code with the side-effect module yet. Do not surface truncation banners.

**Tech Stack:** Python 3.10+, pytest, existing `CodebaseIndex` / `_FakeReader` / `DiskRepoReader`

## Global Constraints

- Work only in worktree `.worktrees/5444-find-references-repo-reader` on branch `feature/5444-find-references-repo-reader`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:`) on new/updated public and helper APIs
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Do not modify `_build_tools`, prompts, or `search_codebase`
- Do not import from `side_effect_impact_pass`; do not refactor `_search_repository`
- Do not attach truncation banners or excerpts in the returned string
- Hit format remains `path:line` only; empty message remains `No references for {symbol!r}.`

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | Constants, `_search_repo_references`, extend `find_references` |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Repo-hit / merge / skip / scan-cap / no-reader tests |

---

### Task 1: Repo half of `find_references` with caps

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Test: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` (insert after existing `find_references` tests, before `# --------------------------------------------------------------------------- tools`)

**Interfaces:**
- Consumes: `CodebaseIndex.search`, `CodebaseIndex.files`, `CodebaseIndex.repo_reader`, `DiskRepoReader`, `DEFAULT_MAX_LISTED_FILES` from `.repo_reader`
- Produces:
  - `_REPO_SEARCH_FILE_SCAN_LIMIT = 40`
  - `_DISK_REPO_SEARCH_FILE_SCAN_LIMIT = DEFAULT_MAX_LISTED_FILES`
  - `_search_repo_references(index, query, max_matches, max_files_scanned=None) -> List[Tuple[str, int, str]]`
  - Updated `CodebaseIndex.find_references(self, symbol: str, max_matches: int = _SEARCH_MATCH_LIMIT) -> str`

- [ ] **Step 1: Write the failing tests**

Insert after `test_find_references_rejects_nonpositive_max` (before the tools section). `_FakeReader` is defined later in the same file — that is fine in this module (existing tests already reference classes defined below). Prefer importing `_search_repo_references` once added; until then tests against `find_references` will fail appropriately.

```python
def test_find_references_includes_repo_reader_hits() -> None:
    """When a reader is present, out-of-submission matches appear as path:line."""
    idx = CodebaseIndex(
        files={"changed.py": "x = 1\n"},
        repo_reader=_FakeReader({"other/caller.py": "from changed import x\nx()\n"}),
    )
    result = idx.find_references("changed")
    assert "other/caller.py:1" in result.splitlines()
    assert "No references" not in result


def test_find_references_merges_submission_then_repo_under_cap() -> None:
    """Submission hits come first; total length respects max_matches."""
    idx = CodebaseIndex(
        files={"a.py": "needle\n"},
        repo_reader=_FakeReader(
            {
                "r1.py": "needle\n",
                "r2.py": "needle\n",
                "r3.py": "needle\n",
            }
        ),
    )
    lines = idx.find_references("needle", max_matches=3).splitlines()
    assert lines[0] == "a.py:1"
    assert len(lines) == 3
    assert all(":" in line and "needle" not in line for line in lines)


def test_find_references_skips_submission_paths_in_repo_half() -> None:
    """A reader path that is also a submission key is not double-counted from repo."""
    idx = CodebaseIndex(
        files={"shared.py": "needle\n"},
        repo_reader=_FakeReader(
            {
                "shared.py": "needle\nneedle\n",  # would add extra lines if not skipped
                "only_repo.py": "needle\n",
            }
        ),
    )
    lines = idx.find_references("needle", max_matches=10).splitlines()
    assert lines.count("shared.py:1") == 1
    assert "shared.py:2" not in lines
    assert "only_repo.py:1" in lines


def test_search_repo_references_respects_max_files_scanned() -> None:
    """File-scan cap limits how many non-submission reader files are opened."""
    from software_engineering_team.code_review_agent.false_positive_filter import (
        _search_repo_references,
    )

    reader_files = {f"f{i}.py": "needle\n" for i in range(5)}
    idx = CodebaseIndex(files={"sub.py": "other\n"}, repo_reader=_FakeReader(reader_files))
    hits = _search_repo_references(idx, "needle", max_matches=10, max_files_scanned=2)
    assert len(hits) == 2
    assert {path for path, _, _ in hits} <= set(reader_files)


def test_find_references_no_reader_unchanged() -> None:
    """Without a reader, behavior stays submission-only."""
    idx = CodebaseIndex(files={"a.py": "def foo():\n    pass\n"})
    assert idx.find_references("foo") == "a.py:1"
    assert idx.find_references("zzz") == "No references for 'zzz'."
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5444-find-references-repo-reader/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_includes_repo_reader_hits \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_merges_submission_then_repo_under_cap \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_skips_submission_paths_in_repo_half \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_search_repo_references_respects_max_files_scanned \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_no_reader_unchanged \
  -v
```

Expected: FAIL (missing repo hits / import of `_search_repo_references`). Note: `test_find_references_no_reader_unchanged` may already PASS — that is OK; the others must fail until implementation.

- [ ] **Step 3: Implement constants + helper + extend `find_references`**

1. Change import from `.repo_reader` to:

```python
from .repo_reader import DEFAULT_MAX_LISTED_FILES, DiskRepoReader, RepoReader
```

2. After `_SEARCH_MATCH_LIMIT = 60`, add:

```python
# Cap on how many repository files one find_references repo half will scan when
# the reader's per-fetch cost is unknown/expensive (e.g. GitHub-backed readers).
_REPO_SEARCH_FILE_SCAN_LIMIT = 40

# Cap used instead for DiskRepoReader (no per-file fetch cost) — match the
# reader's own listing bound so alphabetical prefixes are not silently missed.
_DISK_REPO_SEARCH_FILE_SCAN_LIMIT = DEFAULT_MAX_LISTED_FILES
```

3. Add module-level helper immediately before `_strip_numbered_prefixes` (after the `CodebaseIndex` class), modeled on `_search_repository` but returning only the hit list (truncation flag may be computed internally and discarded):

```python
def _search_repo_references(
    index: CodebaseIndex,
    query: str,
    max_matches: int,
    max_files_scanned: Optional[int] = None,
) -> List[Tuple[str, int, str]]:
    """Find case-insensitive substring hits via ``index.repo_reader`` only.

    Preconditions:
        - ``max_matches`` > 0 and, when given, ``max_files_scanned`` > 0.

    Postconditions:
        - Returns ``[]`` when ``repo_reader`` is None or ``query`` is blank.
        - When ``max_files_scanned`` is None, uses ``_DISK_REPO_SEARCH_FILE_SCAN_LIMIT``
          for ``DiskRepoReader`` else ``_REPO_SEARCH_FILE_SCAN_LIMIT``.
        - Skips paths already keys of ``index.files``; returns up to ``max_matches``
          ``(path, 1-based-line, line-text)`` tuples; never raises on reader errors.
    """
    if max_matches <= 0:
        raise ValueError("max_matches must be positive")
    if max_files_scanned is not None and max_files_scanned <= 0:
        raise ValueError("max_files_scanned must be positive")
    if index.repo_reader is None:
        return []
    if max_files_scanned is None:
        max_files_scanned = (
            _DISK_REPO_SEARCH_FILE_SCAN_LIMIT
            if isinstance(index.repo_reader, DiskRepoReader)
            else _REPO_SEARCH_FILE_SCAN_LIMIT
        )
    needle = (query or "").strip().lower()
    if not needle:
        return []
    try:
        paths = index.repo_reader.list_files()
    except Exception as exc:  # noqa: BLE001 - fail-safe
        logger.debug("find_references: repo_reader.list_files() failed: %s", exc)
        return []

    results: List[Tuple[str, int, str]] = []
    scanned = 0
    for path in paths:
        if path in index.files:
            continue
        if scanned >= max_files_scanned:
            return results
        scanned += 1
        try:
            content = index.repo_reader.read_file(path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("find_references: repo_reader.read_file(%r) failed: %s", path, exc)
            continue
        if content is None:
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            if needle in line.lower():
                results.append((path, lineno, line.rstrip()))
                if len(results) >= max_matches:
                    return results
    return results
```

4. Replace `find_references` body and docstring postconditions to include the repo half:

```python
    def find_references(
        self, symbol: str, max_matches: int = _SEARCH_MATCH_LIMIT
    ) -> str:
        """Search submission (and repo_reader when present) for capped path:line hits.

        Submission matches come from :meth:`search` first. When a ``repo_reader``
        is attached and slots remain under ``max_matches``, fills them from the
        repository (skipping submission paths) via ``_search_repo_references``.
        Does not attach excerpts or truncation banners.

        Preconditions:
            - ``max_matches`` > 0.

        Postconditions:
            - On hits, returns newline-joined ``path:line`` strings (submission
              first, then repo), total length ≤ ``max_matches``, no line text.
            - On no hits (including blank/whitespace-only ``symbol``), returns
              ``No references for {symbol!r}.``
            - Never raises for missing symbols or reader failures; raises
              ``ValueError`` when ``max_matches`` is non-positive (via ``search``).
        """
        hits = list(self.search(symbol, max_matches=max_matches))
        remaining = max_matches - len(hits)
        if remaining > 0 and self.repo_reader is not None:
            hits.extend(
                _search_repo_references(self, symbol, max_matches=remaining)
            )
        if not hits:
            return f"No references for {symbol!r}."
        return "\n".join(f"{path}:{lineno}" for path, lineno, _text in hits)
```

- [ ] **Step 4: Run tests to verify they pass**

Same pytest selector as Step 2 — all must PASS.

Full suite regression:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Extend find_references with capped repo_reader search.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Extend `find_references` one API | Task 1 Step 3 |
| Submission first, fill remaining | Task 1 Steps 1 + 3 |
| Local `_search_repo_references` | Task 1 Step 3 |
| Match + file-scan caps | Task 1 Steps 1 + 3 |
| Skip `index.files` paths | Task 1 Steps 1 + 3 |
| No truncation banner / excerpts | Global constraints |
| No-reader unchanged | Task 1 Step 1 |
| Fake reader unit tests | Task 1 Step 1 |
| Do not touch side-effect `_search_repository` | Global constraints |
