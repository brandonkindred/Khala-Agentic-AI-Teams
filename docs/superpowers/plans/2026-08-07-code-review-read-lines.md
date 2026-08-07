# Code-review `read_lines` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `read_lines(path, start, end)` on `CodebaseIndex` and expose it via `_build_tools` with a hard 400-line max span and explicit error strings.

**Architecture:** Implement range validation and slice formatting on `CodebaseIndex.read_lines` (same path resolution as `read_file`). Register a thin strands `@tool` wrapper in `_build_tools` so FP / architecture / side-effect / merged passes inherit it automatically. No prompt changes.

**Tech Stack:** Python 3.10+, pytest, strands `@tool`, existing `CodebaseIndex` in `false_positive_filter.py`

## Global Constraints

- Work only in worktree `.worktrees/5403-read-lines-hard-max-span` on branch `5403-read-lines-hard-max-span`
- Follow design `docs/superpowers/specs/2026-08-07-code-review-read-lines-design.md`
- Design-by-Contract docstrings (`Preconditions:` / `Postconditions:`) on `CodebaseIndex.read_lines`
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Tool wrappers never raise — return `Error: ...` strings
- Do not rewrite prompts; do not implement `read_function` / `find_references`

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | `_READ_LINES_MAX_SPAN`, `CodebaseIndex.read_lines`, `@tool read_lines` in `_build_tools` |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Contract tests + update 4-tool unpack sites to 5 tools |

---

### Task 1: `CodebaseIndex.read_lines` with hard max span

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Test: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py`

**Interfaces:**
- Consumes: `CodebaseIndex._read` / `read_file_or_none`, `resolve_path`, existing `Error:` path messages
- Produces:
  - Module constant `_READ_LINES_MAX_SPAN: int = 400`
  - `CodebaseIndex.read_lines(self, path: str, start: int, end: int) -> str`

- [ ] **Step 1: Write the failing tests**

Add after the existing `read_file` / `read_file_or_none` tests (near the `# --------------------------------------------------------------------------- CodebaseIndex` section), importing `_READ_LINES_MAX_SPAN` alongside other symbols:

```python
from code_review_agent.false_positive_filter import (
    # ... existing imports ...
    _READ_LINES_MAX_SPAN,
)


def test_read_lines_returns_inclusive_numbered_slice() -> None:
    """Valid range returns header + numbered body for only the requested lines."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\nc\nd\ne\n"})
    result = idx.read_lines("app/main.py", 2, 4)
    assert result.startswith("app/main.py lines 2–4 (3 lines):")
    assert "2| b" in result
    assert "3| c" in result
    assert "4| d" in result
    assert "1| a" not in result
    assert "5| e" not in result


def test_read_lines_inverted_range_errors() -> None:
    """start > end returns an explicit inverted-range error."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\nc\n"})
    msg = idx.read_lines("app/main.py", 3, 1)
    assert msg.startswith("Error:")
    assert "invalid range" in msg
    assert "start (3) > end (1)" in msg


def test_read_lines_oversize_span_errors() -> None:
    """Span larger than _READ_LINES_MAX_SPAN returns an explicit oversize error."""
    body = "\n".join(f"line-{i}" for i in range(1, 500)) + "\n"
    idx = CodebaseIndex(files={"big.py": body})
    span = _READ_LINES_MAX_SPAN + 1
    msg = idx.read_lines("big.py", 1, span)
    assert msg.startswith("Error:")
    assert f"range spans {span} lines" in msg
    assert f"maximum is {_READ_LINES_MAX_SPAN}" in msg


def test_read_lines_clamps_end_past_eof() -> None:
    """end past EOF clamps to the last line when start is in range."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\nc\n"})
    result = idx.read_lines("app/main.py", 2, 99)
    assert result.startswith("app/main.py lines 2–3 (2 lines):")
    assert "2| b" in result
    assert "3| c" in result


def test_read_lines_start_past_eof_errors() -> None:
    """start beyond file length returns an explicit beyond-EOF error."""
    idx = CodebaseIndex(files={"app/main.py": "a\nb\n"})
    msg = idx.read_lines("app/main.py", 5, 6)
    assert msg.startswith("Error:")
    assert "beyond the end" in msg
    assert "file has 2 lines" in msg


def test_read_lines_rejects_non_positive_bounds() -> None:
    """Non-positive or non-int start/end return Error strings (never raise)."""
    idx = CodebaseIndex(files={"app/main.py": "a\n"})
    assert "positive integer" in idx.read_lines("app/main.py", 0, 1)
    assert "positive integer" in idx.read_lines("app/main.py", 1, True)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_lines_returns_inclusive_numbered_slice \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_lines_inverted_range_errors \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_lines_oversize_span_errors \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_lines_clamps_end_past_eof \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_lines_start_past_eof_errors \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_lines_rejects_non_positive_bounds \
  -v
```

Expected: FAIL (import/`AttributeError`: `_READ_LINES_MAX_SPAN` or `read_lines` missing).

- [ ] **Step 3: Implement constant + `CodebaseIndex.read_lines`**

In `false_positive_filter.py`, add near the other caps (after `_SEARCH_MATCH_LIMIT`):

```python
# Hard cap on inclusive line span returned by ``read_lines`` so a tool call
# cannot pull an unbounded slice into the verifier context.
_READ_LINES_MAX_SPAN = 400
```

Add method on `CodebaseIndex` immediately after `read_file_or_none`:

```python
def read_lines(self, path: str, start: int, end: int) -> str:
    """Return an inclusive 1-based line slice of ``path``, capped by max span.

    Preconditions:
        - Callers should pass 1-based inclusive ``start``/``end``. Invalid
          bounds are reported as ``Error: ...`` strings rather than raised.

    Postconditions:
        - Returns ``Error: ...`` for non-positive/non-int bounds, inverted
          ranges, spans above ``_READ_LINES_MAX_SPAN``, unreadable paths, or
          ``start`` past EOF — never raises on those cases.
        - On success, returns a header ``{path} lines {start}–{end_eff} ({n} lines):``
          followed by ``N| content`` body lines for the inclusive slice.
        - When ``end`` exceeds file length and ``start`` is in range, clamps
          ``end`` to the last line.
        - Path resolution matches ``read_file``.
    """
    if not isinstance(start, int) or isinstance(start, bool) or start < 1:
        return f"Error: start must be a positive integer, got {start!r}."
    if not isinstance(end, int) or isinstance(end, bool) or end < 1:
        return f"Error: end must be a positive integer, got {end!r}."
    if start > end:
        return f"Error: invalid range: start ({start}) > end ({end})."
    span = end - start + 1
    if span > _READ_LINES_MAX_SPAN:
        return (
            f"Error: range spans {span} lines; maximum is {_READ_LINES_MAX_SPAN}. "
            "Narrow start/end or use read_function."
        )

    content, error = self._read(path)
    if content is None:
        return error if error is not None else f"Error: file not found: {path}."

    lines = content.splitlines()
    n_lines = len(lines)
    if start > n_lines:
        display = self.resolve_path(path) or path
        return (
            f"Error: start line {start} is beyond the end of {display} "
            f"(file has {n_lines} lines)."
        )
    end_eff = min(end, n_lines)
    display = self.resolve_path(path) or path
    if display == self.EXISTING_CODEBASE_PATH:
        display = path
    n = end_eff - start + 1
    header = f"{display} lines {start}–{end_eff} ({n} lines):"
    body = "\n".join(f"{i}| {lines[i - 1]}" for i in range(start, end_eff + 1))
    return f"{header}\n{body}"
```

- [ ] **Step 4: Run tests to verify they pass**

Same pytest command as Step 2.

Expected: PASS for all six tests.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Add CodebaseIndex.read_lines with a 400-line hard max span.

Enable bounded inclusive slices for code-review tools without loading whole files.
EOF
)"
```

---

### Task 2: Expose `read_lines` via `_build_tools`

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` (`_build_tools`)
- Modify: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` (tool unpack sites + tool tests)

**Interfaces:**
- Consumes: `CodebaseIndex.read_lines(path, start, end) -> str` from Task 1
- Produces: fifth tool in `_build_tools` return list, order:
  `[read_file, read_lines, list_files, search_codebase, find_function_at_line]`

- [ ] **Step 1: Write / update failing tool tests**

Update `test_build_tools_delegate_to_index` and related unpack sites. Tool order after `read_file` inserts `read_lines`, so every `_, _, _, find_*` becomes `_, _, _, _, find_*`, and `_, list_files, _, _` becomes `_, _, list_files, _, _`.

Replace the body of `test_build_tools_delegate_to_index` with:

```python
def test_build_tools_delegate_to_index() -> None:
    """``_build_tools`` returns five tools that delegate to the index."""
    idx = CodebaseIndex(files={"app/main.py": "def foo(): pass\n"}, existing_codebase="old")
    read_file, read_lines, list_files, search_codebase, find_function_at_line = _build_tools(idx)
    assert {
        read_file.tool_name,
        read_lines.tool_name,
        list_files.tool_name,
        search_codebase.tool_name,
        find_function_at_line.tool_name,
    } == {
        "read_file",
        "read_lines",
        "list_files",
        "search_codebase",
        "find_function_at_line",
    }
    assert read_file("app/main.py") == "def foo(): pass\n"
    listed = list_files()
    assert "app/main.py" in listed and CodebaseIndex.EXISTING_CODEBASE_PATH in listed
    assert "app/main.py:1: def foo(): pass" in search_codebase("foo")
    assert "No matches" in search_codebase("zzz-not-there")
    slice_text = read_lines("app/main.py", 1, 1)
    assert slice_text.startswith("app/main.py lines 1–1 (1 lines):")
    assert "1| def foo(): pass" in slice_text
```

Update empty-index list tool unpack:

```python
def test_list_files_tool_handles_empty_index() -> None:
    """The list_files tool returns a placeholder string for an empty index."""
    _, _, list_files, _, _ = _build_tools(CodebaseIndex(files={}))
    assert list_files() == "(no files available)"
```

Update `test_build_tools_never_raise_on_index_errors` to include `read_lines`:

```python
def test_build_tools_never_raise_on_index_errors(monkeypatch) -> None:
    """Index-backed tools return Error strings when the underlying index raises."""
    idx = CodebaseIndex(files={"a.py": "x"})
    read_file, read_lines, list_files, search_codebase, _find = _build_tools(idx)

    def _boom_read(_self: CodebaseIndex, path: str) -> str:
        raise RuntimeError("index boom")

    def _boom_read_lines(_self: CodebaseIndex, path: str, start: int, end: int) -> str:
        raise RuntimeError("index boom")

    def _boom_list(_self: CodebaseIndex) -> List[str]:
        raise RuntimeError("index boom")

    def _boom_search(_self: CodebaseIndex, query: str, max_matches: int = 60):
        raise RuntimeError("index boom")

    monkeypatch.setattr(CodebaseIndex, "read_file", _boom_read)
    monkeypatch.setattr(CodebaseIndex, "read_lines", _boom_read_lines)
    monkeypatch.setattr(CodebaseIndex, "list_files", _boom_list)
    monkeypatch.setattr(CodebaseIndex, "search", _boom_search)

    assert read_file("a.py").startswith("Error:")
    assert read_lines("a.py", 1, 1).startswith("Error:")
    assert list_files().startswith("Error:")
    assert search_codebase("x").startswith("Error:")
```

Bulk-update every remaining `_, _, _, find` unpack in this file to `_, _, _, _, find` (and `find_fn` variants). There are ~20 sites; mechanical 4-tuple → 5-tuple with an extra `_` before the find tool.

Add one dedicated tool-level contract test:

```python
def test_read_lines_tool_enforces_max_span() -> None:
    """The read_lines tool surfaces the oversize-span error from the index."""
    body = "\n".join(f"L{i}" for i in range(1, 450)) + "\n"
    idx = CodebaseIndex(files={"big.py": body})
    _, read_lines, _, _, _ = _build_tools(idx)
    msg = read_lines("big.py", 1, _READ_LINES_MAX_SPAN + 1)
    assert msg.startswith("Error:")
    assert f"maximum is {_READ_LINES_MAX_SPAN}" in msg
```

- [ ] **Step 2: Run tests to verify unpack failures / missing tool**

Run:

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -q --tb=line
```

Expected: FAIL on ValueError (not enough values to unpack) and/or missing `read_lines` tool name until Step 3.

- [ ] **Step 3: Register the tool in `_build_tools`**

Update `_build_tools` docstring postcondition to five tools. Insert after `read_file`:

```python
@tool
def read_lines(path: str, start: int, end: int) -> str:
    """Read an inclusive 1-based line range from a file under review.

    Prefer this over read_file when you only need a bounded slice. The
    maximum span is 400 lines; use a narrower range or read_function for
    larger constructs.

    Args:
        path: File path (same paths accepted by read_file).
        start: 1-based inclusive start line.
        end: 1-based inclusive end line.

    Returns:
        A header plus ``N| content`` lines, or an ``Error: ...`` message.
    """
    try:
        return index.read_lines(path, start, end)
    except Exception as exc:
        return (
            f"Error: could not read_lines {path!r} [{start}:{end}]: "
            f"{type(exc).__name__}: {exc}"
        )
```

Change the return to:

```python
return [read_file, read_lines, list_files, search_codebase, find_function_at_line]
```

- [ ] **Step 4: Run full FP-filter suite**

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -q --tb=short
```

Expected: all tests PASS (prior baseline was 100; expect ~107+).

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Expose read_lines on the shared code-review tool surface.

Register the bounded-slice tool in _build_tools so verification passes inherit it.
EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| `CodebaseIndex.read_lines` | Task 1 |
| `_READ_LINES_MAX_SPAN = 400` | Task 1 |
| Inclusive 1-based range | Task 1 |
| Inverted / oversize / bad-type errors | Task 1 |
| Clamp `end` past EOF; error on `start` past EOF | Task 1 |
| Header + `N\| content` success format | Task 1 |
| Path resolution via `_read` | Task 1 |
| Tool in `_build_tools`; never raises | Task 2 |
| Unit tests for valid / inverted / oversize | Tasks 1–2 |
| Prompt / `read_function` / pass wiring | Out of scope (intentionally omitted) |
