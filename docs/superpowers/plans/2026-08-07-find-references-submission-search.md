# find_references Submission Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `CodebaseIndex.find_references(symbol)` that returns capped `path:line` hits from the in-memory submission/index (or a clear empty message).

**Architecture:** Thin wrapper over existing `CodebaseIndex.search`: reuse its case-insensitive substring scan, corpus (submission files + existing-codebase excerpt), and `_SEARCH_MATCH_LIMIT` cap; format hits as `path:line` only. No tool wiring, repo reader, truncation banners, or excerpts.

**Tech Stack:** Python 3.10+, pytest, existing `CodebaseIndex` in `false_positive_filter.py`

## Global Constraints

- Work only in worktree `.worktrees/5443-find-references-submission` on branch `feature/5443-find-references-submission`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:`) on the new public method
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Do not modify `_build_tools`, prompts, or `search_codebase`
- Do not consult `repo_reader` inside `find_references`

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | Add `CodebaseIndex.find_references` immediately after `search` |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Unit tests for hits, empty, blank, cap, precondition |

---

### Task 1: `CodebaseIndex.find_references` (submission path:line hits)

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` (insert after `search`, currently ending ~line 695)
- Test: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` (insert after `test_search_rejects_nonpositive_max`, before the `# tools` section ~line 558)

**Interfaces:**
- Consumes: `CodebaseIndex.search(query: str, max_matches: int = _SEARCH_MATCH_LIMIT) -> List[Tuple[str, int, str]]`; `_SEARCH_MATCH_LIMIT`
- Produces: `CodebaseIndex.find_references(self, symbol: str, max_matches: int = _SEARCH_MATCH_LIMIT) -> str`

- [ ] **Step 1: Write the failing tests**

Insert after `test_search_rejects_nonpositive_max` (before `# --------------------------------------------------------------------------- tools`):

```python
def test_find_references_returns_capped_path_line_hits() -> None:
    """Hits are path:line only (no line text), across files and the excerpt."""
    idx = CodebaseIndex(
        files={
            "a.py": "def foo():\n    pass\n",
            "b.py": "FOO_CONST = 1\n",
        },
        existing_codebase="legacy_foo()\n",
    )
    result = idx.find_references("foo")
    assert result == (
        "a.py:1\n"
        "b.py:1\n"
        f"{CodebaseIndex.EXISTING_CODEBASE_PATH}:1"
    )


def test_find_references_empty_and_blank_symbol() -> None:
    """Unknown or whitespace-only symbol returns the empty-references message."""
    idx = CodebaseIndex(files={"a.py": "def foo():\n    pass\n"})
    assert idx.find_references("zzz-not-there") == "No references for 'zzz-not-there'."
    assert idx.find_references("   ") == "No references for '   '."


def test_find_references_respects_max_matches() -> None:
    """Result is capped at max_matches path:line lines."""
    idx = CodebaseIndex(files={"a.py": "x\n" * 100})
    result = idx.find_references("x", max_matches=5)
    assert result.splitlines() == [f"a.py:{i}" for i in range(1, 6)]


@pytest.mark.parametrize("bad_max", [0, -1, -100])
def test_find_references_rejects_nonpositive_max(bad_max: int) -> None:
    """Non-positive max_matches raises ValueError (same precondition as search)."""
    with pytest.raises(ValueError):
        CodebaseIndex(files={"a.py": "x"}).find_references("x", max_matches=bad_max)
```

- [ ] **Step 2: Run tests to verify they fail**

Run from worktree `backend/` using the main repo venv:

```bash
cd /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/5443-find-references-submission/backend
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_returns_capped_path_line_hits \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_empty_and_blank_symbol \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_respects_max_matches \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_find_references_rejects_nonpositive_max \
  -v
```

Expected: FAIL with `AttributeError: 'CodebaseIndex' object has no attribute 'find_references'` (or similar).

- [ ] **Step 3: Implement `find_references`**

Insert on `CodebaseIndex` immediately after `search` (before `_strip_numbered_prefixes`):

```python
    def find_references(
        self, symbol: str, max_matches: int = _SEARCH_MATCH_LIMIT
    ) -> str:
        """Search in-memory sources for ``symbol`` and return capped path:line hits.

        Thin wrapper over :meth:`search`: same corpus (submission files plus the
        existing-codebase excerpt), case-insensitive substring match, and cap.
        Does not consult the repo reader and does not attach excerpts.

        Preconditions:
            - ``max_matches`` > 0.

        Postconditions:
            - On hits, returns newline-joined ``path:line`` strings for the first
              ``max_matches`` occurrences in path-then-line order (no line text).
            - On no hits (including a blank/whitespace-only ``symbol``), returns
              ``No references for {symbol!r}.``
            - Never raises for missing symbols; raises ``ValueError`` when
              ``max_matches`` is non-positive (delegated via ``search``).
        """
        hits = self.search(symbol, max_matches=max_matches)
        if not hits:
            return f"No references for {symbol!r}."
        return "\n".join(f"{path}:{lineno}" for path, lineno, _text in hits)
```

- [ ] **Step 4: Run tests to verify they pass**

Same pytest command as Step 2.

Expected: all four tests PASS.

Also re-run the full false-positive filter suite for regressions:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -q
```

Expected: all tests PASS (prior count + 4 new; parametrize expands the nonpositive case to 3).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Add CodebaseIndex.find_references for capped submission path:line hits.

EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Thin wrapper over `search` | Task 1 Step 3 |
| `path:line` only | Task 1 Steps 1 + 3 |
| Empty message `No references for {symbol!r}.` | Task 1 Steps 1 + 3 |
| Corpus = submission + excerpt | Inherited via `search`; asserted in hits test |
| Cap via `max_matches` / `_SEARCH_MATCH_LIMIT` | Task 1 Steps 1 + 3 |
| Blank → empty message | Task 1 Steps 1 + 3 |
| `max_matches <= 0` → `ValueError` | Task 1 Steps 1 + 3 |
| No repo_reader / tool / excerpts / prompts | Global constraints; not touched |
