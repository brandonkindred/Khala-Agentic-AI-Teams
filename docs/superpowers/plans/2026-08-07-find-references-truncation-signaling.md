# find_references Truncation Signaling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `find_references` explicitly signal truncated scans and no-`repo_reader` cases so agents never treat partial/unavailable search as “no references exist.”

**Architecture:** Change `_search_repo_references` to return `(hits, truncated)` with the same truncated semantics as `side_effect_impact_pass._search_repository`. Format banners and no-reader notes in `find_references`, mirroring `search_repository` wording. Treat “submission already filled `max_matches`” as truncated when a reader is present.

**Tech Stack:** Python 3.10+, pytest, existing `CodebaseIndex` / `_FakeReader`

## Global Constraints

- Work only in worktree `.worktrees/5445-find-references-truncation` on branch `feature/5445-find-references-truncation`
- Design-by-Contract docstrings on updated APIs
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Do not modify `_build_tools`, prompts, or `search_codebase`
- Do not import from / refactor `side_effect_impact_pass`
- Hit lines remain `path:line` only (banners are separate trailing text)

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | Truncation bool + messaging in `find_references` / `_search_repo_references` |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Update existing asserts; add truncation / no-reader tests |

### Exact message strings (use verbatim)

No-reader note (always append when `repo_reader is None`):

```text
No repository access is available beyond this submission.
```

(Append after the base body, separated by `\n\n` when the body is non-empty hits; for empty base, prefer body then note, or `No references for {symbol!r}.\n\n` + note.)

Truncated hits banner (append when hits and truncated):

```text

(Scan truncated before covering the whole repository -- there may be more matches for {symbol!r} beyond what's shown above.)
```

Truncated empty (when no hits and truncated and reader present):

```text
No references for {symbol!r} in the files scanned, but the scan was truncated before covering the whole repository -- this does NOT prove the symbol is absent elsewhere. Use list_files()/read_file() for a more targeted follow-up if this matters.
```

(Complete empty with reader: keep `No references for {symbol!r}.`)

---

### Task 1: Truncation + no-reader signaling

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Modify: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py`

**Interfaces:**
- Consumes: existing `_search_repo_references`, `DiskRepoReader.listing_truncated` when available
- Produces: `_search_repo_references(...) -> Tuple[List[Tuple[str, int, str]], bool]`; updated `find_references` string contract

- [ ] **Step 1: Update existing tests + add failing new tests**

Update no-reader exact asserts to include the note:

```python
_NO_REPO = "No repository access is available beyond this submission."

def test_find_references_empty_and_blank_symbol() -> None:
    idx = CodebaseIndex(files={"a.py": "def foo():\n    pass\n"})
    assert idx.find_references("zzz-not-there") == (
        f"No references for 'zzz-not-there'.\n\n{_NO_REPO}"
    )
    assert idx.find_references("   ") == f"No references for '   '.\n\n{_NO_REPO}"


def test_find_references_returns_capped_path_line_hits() -> None:
    # ... same setup ...
    result = idx.find_references("foo")
    assert result.startswith(
        "a.py:1\n"
        "b.py:1\n"
        f"{CodebaseIndex.EXISTING_CODEBASE_PATH}:1"
    )
    assert _NO_REPO in result


def test_find_references_no_reader_unchanged() -> None:
    """Without a reader, results stay submission-only and note that explicitly."""
    idx = CodebaseIndex(files={"a.py": "def foo():\n    pass\n"})
    assert idx.find_references("foo") == f"a.py:1\n\n{_NO_REPO}"
    assert idx.find_references("zzz") == f"No references for 'zzz'.\n\n{_NO_REPO}"
```

Update helper test to unpack the tuple:

```python
def test_search_repo_references_respects_max_files_scanned() -> None:
    from software_engineering_team.code_review_agent.false_positive_filter import (
        _search_repo_references,
    )

    reader_files = {f"f{i}.py": "needle\n" for i in range(5)}
    idx = CodebaseIndex(files={"sub.py": "other\n"}, repo_reader=_FakeReader(reader_files))
    hits, truncated = _search_repo_references(
        idx, "needle", max_matches=10, max_files_scanned=2
    )
    assert len(hits) == 2
    assert truncated is True
    assert {path for path, _, _ in hits} <= set(reader_files)
```

Add:

```python
def test_find_references_no_reader_note_on_hits() -> None:
    idx = CodebaseIndex(files={"a.py": "foo\n"})
    result = idx.find_references("foo")
    assert result.startswith("a.py:1")
    assert _NO_REPO in result


def test_find_references_truncated_banner_when_match_cap_skips_repo() -> None:
    """Submission fills max_matches with a reader present → truncated (repo not searched)."""
    idx = CodebaseIndex(
        files={"a.py": "x\nx\nx\n"},
        repo_reader=_FakeReader({"r.py": "x\n"}),
    )
    result = idx.find_references("x", max_matches=2)
    lines = result.split("\n\n", 1)[0].splitlines()
    assert lines == ["a.py:1", "a.py:2"]
    assert "Scan truncated" in result
    assert "more matches" in result


def test_find_references_truncated_empty_message(monkeypatch) -> None:
    """Repo scan hits file-scan cap with no matches → empty-truncated wording."""
    import software_engineering_team.code_review_agent.false_positive_filter as fpf

    monkeypatch.setattr(fpf, "_REPO_SEARCH_FILE_SCAN_LIMIT", 2)
    idx = CodebaseIndex(
        files={"sub.py": "other\n"},
        repo_reader=_FakeReader({f"f{i}.py": "zzz\n" for i in range(5)}),
    )
    result = idx.find_references("needle")
    assert "No references for 'needle'" in result
    assert "truncated" in result
    assert "does NOT prove" in result
```

Also rename/replace `test_find_references_no_reader_unchanged` as shown above (do not leave a duplicate).

For merge-cap tests with a reader present that fill the cap from submission+repo: if `max_matches` is reached mid-repo, truncated should be True — assert `"Scan truncated"` in `test_find_references_merges_submission_then_repo_under_cap` (it uses max_matches=3 with more needles available).

- [ ] **Step 2: Run tests to verify RED**

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5445-find-references-truncation/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -k find_references -v
```

Expected: failures on new message/truncation asserts and tuple unpack.

- [ ] **Step 3: Implement**

1. Change `_search_repo_references` to return `Tuple[List[Tuple[str, int, str]], bool]`, setting `incomplete` like `_search_repository`:
   - Disk `listing_truncated()`
   - `list_files` failure → `([], True)`
   - file-scan cap hit → `(results, True)`
   - match cap hit → `(results, True)`
   - read errors / `None` content → `incomplete = True`
   - blank query / no reader → `([], False)`

2. Rewrite `find_references`:

```python
    def find_references(
        self, symbol: str, max_matches: int = _SEARCH_MATCH_LIMIT
    ) -> str:
        """Search submission (and repo_reader when present) for capped path:line hits.

        ...
        Postconditions:
            - On complete hits with a reader: newline-joined ``path:line`` only.
            - When truncated (repo scan incomplete, or submission filled
              ``max_matches`` so the repo half was skipped): append a truncated
              banner (hits) or an empty-truncated message (no hits).
            - When no ``repo_reader``: always append the no-repository-access note.
            - Never raises for missing symbols or reader failures; raises
              ``ValueError`` when ``max_matches`` is non-positive (via ``search``).
        """
        hits = list(self.search(symbol, max_matches=max_matches))
        truncated = False
        if self.repo_reader is None:
            body = (
                "\n".join(f"{path}:{lineno}" for path, lineno, _ in hits)
                if hits
                else f"No references for {symbol!r}."
            )
            return f"{body}\n\nNo repository access is available beyond this submission."

        remaining = max_matches - len(hits)
        if remaining == 0:
            truncated = True
        elif remaining > 0:
            repo_hits, repo_truncated = _search_repo_references(
                self, symbol, max_matches=remaining
            )
            hits.extend(repo_hits)
            truncated = repo_truncated

        if not hits:
            if truncated:
                return (
                    f"No references for {symbol!r} in the files scanned, but the scan was "
                    "truncated before covering the whole repository -- this does NOT prove "
                    "the symbol is absent elsewhere. Use list_files()/read_file() for a "
                    "more targeted follow-up if this matters."
                )
            return f"No references for {symbol!r}."

        result = "\n".join(f"{path}:{lineno}" for path, lineno, _ in hits)
        if truncated:
            result += (
                f"\n\n(Scan truncated before covering the whole repository -- there may be "
                f"more matches for {symbol!r} beyond what's shown above.)"
            )
        return result
```

- [ ] **Step 4: Run tests GREEN**

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
Signal find_references truncation and no-reader limits explicitly.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| `(hits, truncated)` helper | Task 1 Step 3 |
| Match-cap-before-repo → truncated | Task 1 Steps 1 + 3 |
| Truncation banners / empty-truncated | Task 1 Steps 1 + 3 |
| Always no-reader note | Task 1 Steps 1 + 3 |
| Update existing exact-string tests | Task 1 Step 1 |
| Unit tests for both behaviors | Task 1 Step 1 |
