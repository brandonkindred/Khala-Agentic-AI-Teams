# Code-review `read_function` by line Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `CodebaseIndex.read_function(path, line)` that returns the enclosing Python construct body (header + numbered lines) or a clear `Error:` string.

**Architecture:** Resolve path via `_read`, require `.py`/`.pyi`, strip annotated hunk prefixes when present, call `enclosing_construct`, then format the inclusive construct span like `read_lines`. No tool registration in this task.

**Tech Stack:** Python 3.10+, pytest, existing `function_boundaries.enclosing_construct` / `strip_numbered_prefixes`

## Global Constraints

- Work only in worktree `.worktrees/5441-read-function-by-line` on branch `5441-read-function-by-line`
- Follow design `docs/superpowers/specs/2026-08-07-code-review-read-function-by-line-design.md`
- Design-by-Contract docstring on `CodebaseIndex.read_function`
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Do not register a strands tool; do not implement name resolution; do not rewrite prompts
- Do not apply `_READ_LINES_MAX_SPAN` to construct bodies

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | `CodebaseIndex.read_function` |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Contract tests |

---

### Task 1: `CodebaseIndex.read_function` by line

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Test: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py`

**Interfaces:**
- Consumes: `CodebaseIndex._read`, `resolve_path`, `strip_numbered_prefixes`, `enclosing_construct`
- Produces: `CodebaseIndex.read_function(self, path: str, line: int) -> str`

- [ ] **Step 1: Write the failing tests**

Add after the existing `test_read_lines_*` cases (around line 325):

```python
def test_read_function_returns_method_in_class_body() -> None:
    """Line inside a method returns only that method's construct body."""
    src = (
        "class C:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "def other():\n"
        "    return 2\n"
    )
    idx = CodebaseIndex(files={"app/mod.py": src})
    # Line 3 is inside C.m
    result = idx.read_function("app/mod.py", 3)
    assert result.startswith("app/mod.py function C.m lines 2–3 (2 lines):")
    assert "2|     def m(self):" in result
    assert "3|         return 1" in result
    assert "class C" not in result.split("\n", 1)[1]  # body excludes class header
    assert "def other" not in result


def test_read_function_unresolved_module_level_errors() -> None:
    """Module-level line with no enclosing construct returns a clear error."""
    idx = CodebaseIndex(files={"app/mod.py": "x = 1\n\ndef f():\n    return x\n"})
    msg = idx.read_function("app/mod.py", 1)
    assert msg.startswith("Error:")
    assert "no enclosing function/class" in msg
    assert "line 1" in msg


def test_read_function_non_python_errors() -> None:
    """Non-Python paths return a clear Python-only error."""
    idx = CodebaseIndex(files={"app/main.ts": "function f() { return 1; }\n"})
    msg = idx.read_function("app/main.ts", 1)
    assert msg.startswith("Error:")
    assert "Python file" in msg
    assert "app/main.ts" in msg


def test_read_function_rejects_non_positive_line() -> None:
    """Non-positive or non-int line returns Error (never raises)."""
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    assert "positive integer" in idx.read_function("app/mod.py", 0)
    assert "positive integer" in idx.read_function("app/mod.py", True)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_function_returns_method_in_class_body \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_function_unresolved_module_level_errors \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_function_non_python_errors \
  agents/software_engineering_team/tests/test_false_positive_filter.py::test_read_function_rejects_non_positive_line \
  -v
```

Expected: FAIL (`AttributeError: read_function`).

- [ ] **Step 3: Implement `CodebaseIndex.read_function`**

Insert immediately after `read_lines` (before `search`):

```python
def read_function(self, path: str, line: int) -> str:
    """Return the enclosing Python construct body for ``line``, or an error.

    Preconditions:
        - Callers should pass a 1-based ``line``. Invalid bounds and
          unresolved lookups are reported as ``Error: ...`` strings rather
          than raised.

    Postconditions:
        - Returns ``Error: ...`` for bad ``line``, unreadable paths,
          non-``.py``/``.pyi`` paths, or when no enclosing function/class
          brackets ``line`` — never raises on those cases.
        - On success, returns a header
          ``{path} {kind} {name} lines {start}–{end} ({n} lines):``
          followed by ``N| content`` body lines for the inclusive construct
          span (decorators included). Path resolution matches ``read_file``.
        - Does not apply ``_READ_LINES_MAX_SPAN``.
    """
    if not isinstance(line, int) or isinstance(line, bool) or line < 1:
        return f"Error: line must be a positive integer, got {line!r}."

    content, error = self._read(path)
    if content is None:
        return error if error is not None else f"Error: file not found: {path}."

    display = self.resolve_path(path) or path
    if display == self.EXISTING_CODEBASE_PATH:
        display = path
    _, ext = os.path.splitext(display)
    if ext.lower() not in (".py", ".pyi"):
        return (
            f"Error: read_function by line requires a Python file (.py/.pyi); "
            f"got {display}."
        )

    stripped, physical, mapper = strip_numbered_prefixes(content, line)
    construct = enclosing_construct(
        stripped, physical, annotated_hunks=mapper is not None
    )
    if construct is None:
        return (
            f"Error: no enclosing function/class for line {line} of {display}."
        )

    display_start = (
        mapper(construct.start_line) if mapper is not None else construct.start_line
    )
    display_end = (
        mapper(construct.end_line) if mapper is not None else construct.end_line
    )
    body_lines = stripped.splitlines()
    n = construct.end_line - construct.start_line + 1
    header = (
        f"{display} {construct.kind} {construct.name} "
        f"lines {display_start}–{display_end} ({n} lines):"
    )
    body = "\n".join(
        f"{(mapper(i) if mapper is not None else i)}| {body_lines[i - 1]}"
        for i in range(construct.start_line, construct.end_line + 1)
    )
    return f"{header}\n{body}"
```

Ensure `strip_numbered_prefixes` and `enclosing_construct` remain imported (already are via the existing `function_boundaries` import block). Prefer calling them directly rather than the `_strip_numbered_prefixes` re-export alias.

- [ ] **Step 4: Run tests to verify they pass**

Same pytest command as Step 2, then the full file once before commit:

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py -q --tb=short
```

Expected: all prior tests still pass; four new tests PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py
git commit -m "$(cat <<'EOF'
Add CodebaseIndex.read_function for line-based construct reads.

Resolve the enclosing Python function/class via function_boundaries and return only that body.
EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| `read_function(path, line)` | Task 1 |
| Header + `N\| content` success format | Task 1 |
| Method-in-class test | Task 1 |
| Unresolved / non-Python / bad line errors | Task 1 |
| Uses `enclosing_construct` + annotated strip | Task 1 |
| No tool / name / prompt / span-cap coupling | Out of scope (omitted) |
