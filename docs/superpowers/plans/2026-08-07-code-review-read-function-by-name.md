# Code-review `read_function` by name Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add name-based construct lookup and a unified `read_function` tool that dispatches line vs name, with clear missing/ambiguous errors.

**Architecture:** Add `iter_constructs` in `function_boundaries`. Add `CodebaseIndex.read_function_by_name` plus a shared success formatter with the existing line path. Register one strands tool that dispatches int/digits → line, else name.

**Tech Stack:** Python 3.10+, pytest, strands `@tool`, existing `EnclosingConstruct` / AST helpers

## Global Constraints

- Work only in worktree `.worktrees/5442-read-function-by-name` on branch `5442-read-function-by-name`
- Follow design `docs/superpowers/specs/2026-08-07-code-review-read-function-by-name-design.md`
- Design-by-Contract docstrings on new public functions/methods
- Never put GitHub issue numbers in code, comments, commit messages, or docs (PR body only)
- Ruff line-length 120; Python 3.10 target
- Coverage ≥ 90% on new/changed code
- Exact case-sensitive name match; tool never raises
- Do not rewrite prompts; do not change line-resolution semantics beyond shared formatting extraction

## File map

| File | Role |
|---|---|
| `code_review_agent/function_boundaries.py` | `iter_constructs` |
| `code_review_agent/false_positive_filter.py` | `read_function_by_name`, shared formatter, tool |
| `tests/test_function_boundaries.py` | `iter_constructs` unit tests |
| `tests/test_false_positive_filter.py` | Name + tool tests; 5→6 unpack updates |
| `tests/test_side_effect_impact_pass.py` | Exact tool-set includes `read_function` |

---

### Task 1: `iter_constructs` in `function_boundaries`

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/function_boundaries.py`
- Test: `backend/agents/software_engineering_team/tests/test_function_boundaries.py`

**Interfaces:**
- Consumes: `node_start_line`, `node_end_line`, `EnclosingConstruct`
- Produces: `iter_constructs(content: str) -> List[EnclosingConstruct]`

- [ ] **Step 1: Write the failing tests**

Append to `test_function_boundaries.py`:

```python
from code_review_agent.function_boundaries import iter_constructs


def test_iter_constructs_qualifies_methods_and_lists_all() -> None:
    src = (
        "class C:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "def top():\n"
        "    return 2\n"
    )
    constructs = iter_constructs(src)
    names = {c.name for c in constructs}
    assert names == {"C", "C.m", "top"}
    method = next(c for c in constructs if c.name == "C.m")
    assert method.kind == "function"
    assert method.start_line == 2 and method.end_line == 3


def test_iter_constructs_parse_failure_returns_empty() -> None:
    assert iter_constructs("def broken(\n") == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_function_boundaries.py::test_iter_constructs_qualifies_methods_and_lists_all \
  agents/software_engineering_team/tests/test_function_boundaries.py::test_iter_constructs_parse_failure_returns_empty \
  -v
```

Expected: FAIL (import/`AttributeError`).

- [ ] **Step 3: Implement `iter_constructs`**

In `function_boundaries.py`, after `_enclosing_construct_ast` (or near it), add:

```python
def iter_constructs(content: str) -> List[EnclosingConstruct]:
    """Return every function/method/class construct in ``content``.

    Preconditions:
        - ``content`` is a string (may be empty).

    Postconditions:
        - Returns ``[]`` when ``content`` fails to parse as Python. Never raises.
        - Otherwise returns one ``EnclosingConstruct`` per ``FunctionDef``,
          ``AsyncFunctionDef``, and ``ClassDef``, with method names qualified
          as ``ClassName.method`` when nested in a class body (same rules as
          :func:`enclosing_construct`). Ranges use ``node_start_line`` /
          ``node_end_line`` (decorators included on start).
    """
    try:
        tree = ast.parse(content)
    except Exception:
        return []

    nodes: List[Tuple[int, int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start_line = node_start_line(node)
        end_line = node_end_line(node)
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        nodes.append((start_line, end_line, node.name, kind))

    results: List[EnclosingConstruct] = []
    for start_line, end_line, name, kind in nodes:
        qualified = name
        if kind == "function":
            enclosing_classes = [
                (cend - cstart, cname)
                for cstart, cend, cname, ckind in nodes
                if ckind == "class" and cstart <= start_line and cend >= end_line
            ]
            if enclosing_classes:
                _, class_name = min(enclosing_classes)
                qualified = f"{class_name}.{name}"
        results.append(
            EnclosingConstruct(
                start_line=start_line, end_line=end_line, name=qualified, kind=kind
            )
        )
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Same pytest command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/function_boundaries.py \
  backend/agents/software_engineering_team/tests/test_function_boundaries.py
git commit -m "$(cat <<'EOF'
Add iter_constructs for Python function/class name listing.

Enable exact name lookup for scoped read_function by collecting qualified constructs.
EOF
)"
```

---

### Task 2: `read_function_by_name`, shared formatter, and tool

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Modify: `backend/agents/software_engineering_team/tests/test_false_positive_filter.py`
- Modify: `backend/agents/software_engineering_team/tests/test_side_effect_impact_pass.py`

**Interfaces:**
- Consumes: `iter_constructs`, existing `read_function` line path
- Produces:
  - `CodebaseIndex.read_function_by_name(self, path: str, name: str) -> str`
  - Private `_format_construct_read(...)` (or module-level helper) used by line + name
  - Tool `read_function` in `_build_tools` (6 tools total)

- [ ] **Step 1: Write failing tests**

In `test_false_positive_filter.py`, after existing `test_read_function_*` cases, and import `iter_constructs` only if needed (not required for these):

```python
def test_read_function_by_name_unique_match() -> None:
    src = (
        "class C:\n"
        "    def m(self):\n"
        "        return 1\n"
        "\n"
        "def other():\n"
        "    return 2\n"
    )
    idx = CodebaseIndex(files={"app/mod.py": src})
    by_name = idx.read_function_by_name("app/mod.py", "C.m")
    by_line = idx.read_function("app/mod.py", 3)
    assert by_name == by_line
    assert by_name.startswith("app/mod.py function C.m lines 2–3 (2 lines):")


def test_read_function_by_name_missing_errors() -> None:
    idx = CodebaseIndex(files={"app/mod.py": "def f():\n    return 1\n"})
    msg = idx.read_function_by_name("app/mod.py", "missing")
    assert msg.startswith("Error:")
    assert "no function/class named 'missing'" in msg


def test_read_function_by_name_ambiguous_errors() -> None:
    """Two same-named top-level defs in one AST file → ambiguous exact match."""
    src = (
        "def twin():\n"
        "    return 1\n"
        "\n"
        "def twin():\n"
        "    return 2\n"
    )
    idx = CodebaseIndex(files={"app/mod.py": src})
    msg = idx.read_function_by_name("app/mod.py", "twin")
    assert msg.startswith("Error:")
    assert "ambiguous" in msg
    assert "twin" in msg


def test_read_function_tool_dispatches_line_and_name() -> None:
    src = "def f():\n    return 1\n"
    idx = CodebaseIndex(files={"app/mod.py": src})
    tools = _build_tools(idx)
    names = {t.tool_name for t in tools}
    assert "read_function" in names
    read_function = next(t for t in tools if t.tool_name == "read_function")
    by_line = read_function("app/mod.py", 1)
    by_digit = read_function("app/mod.py", "1")
    by_name = read_function("app/mod.py", "f")
    assert by_line == by_digit == by_name
    assert by_name.startswith("app/mod.py function f lines 1–2")
```

Also update `test_build_tools_delegate_to_index` expected set to six tools including `read_function`, and every 5-tuple unpack to 6-tuples with `read_function` after `read_lines`:

Order: `read_file, read_lines, read_function, list_files, search_codebase, find_function_at_line`

So `_, _, _, _, find` → `_, _, _, _, _, find`
And `_, _, list_files, _, _` → `_, _, _, list_files, _, _`
And `_, read_lines, _, _, _` → `_, read_lines, _, _, _, _`

In `test_side_effect_impact_pass.py`, add `"read_function"` to the expected names set.

Update `test_build_tools_never_raise_on_index_errors` to monkeypatch `read_function_by_name` or the tool path for name, and include `read_function` tool boom coverage via index method raise.

- [ ] **Step 2: Run focused tests — expect FAIL / unpack errors**

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_false_positive_filter.py \
  agents/software_engineering_team/tests/test_side_effect_impact_pass.py::test_build_side_effect_tools_includes_search_repository \
  -q --tb=line
```

- [ ] **Step 3: Implement**

1. Import `iter_constructs` from `.function_boundaries`.
2. Extract shared formatter from `read_function` body formatting into a helper, e.g.:

```python
def _format_construct_slice(
    display: str,
    construct: "EnclosingConstruct",
    body_lines: List[str],
    *,
    mapper: Optional[Callable[[int], int]] = None,
) -> str:
    display_start = mapper(construct.start_line) if mapper is not None else construct.start_line
    display_end = mapper(construct.end_line) if mapper is not None else construct.end_line
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

Refactor `read_function` to call it after resolving `construct`.

3. Add `read_function_by_name`:

```python
def read_function_by_name(self, path: str, name: str) -> str:
    """Return the construct body for an exact name match, or an error.

    Preconditions:
        - ``name`` should be a non-empty string matching ``EnclosingConstruct.name``
          exactly (bare or ``Class.method``).

    Postconditions:
        - Returns ``Error: ...`` for bad name, unreadable/non-Python paths,
          zero matches, or multiple matches — never raises on those cases.
        - On a unique match, returns the same success format as ``read_function``.
    """
    if not isinstance(name, str) or not name.strip():
        return f"Error: name must be a non-empty string, got {name!r}."
    needle = name.strip()

    content, error = self._read(path)
    if content is None:
        return error if error is not None else f"Error: file not found: {path}."

    display = self.resolve_path(path) or path
    if display == self.EXISTING_CODEBASE_PATH:
        display = path
    _, ext = os.path.splitext(display)
    if ext.lower() not in (".py", ".pyi"):
        return (
            f"Error: read_function by name requires a Python file (.py/.pyi); "
            f"got {display}."
        )

    stripped, _, mapper = strip_numbered_prefixes(content, 1)
    matches = [c for c in iter_constructs(stripped) if c.name == needle]
    if not matches:
        return f"Error: no function/class named {needle!r} in {display}."
    if len(matches) > 1:
        detail = ", ".join(
            f"{c.name} (lines {c.start_line}–{c.end_line})" for c in matches
        )
        return f"Error: name {needle!r} is ambiguous in {display}; matches: {detail}."
    return _format_construct_slice(
        display, matches[0], stripped.splitlines(), mapper=mapper
    )
```

4. Register tool after `read_lines`:

```python
@tool
def read_function(path: str, name_or_line) -> str:
    """Read one function/method/class body by line number or exact name.

    Pass a positive integer (or digit string) for line-based lookup, or a
    name such as ``foo`` / ``Class.method`` for exact name lookup.

    Args:
        path: File path (same paths accepted by read_file).
        name_or_line: 1-based line number or exact construct name.

    Returns:
        Header plus ``N| content`` lines, or an ``Error: ...`` message.
    """
    try:
        if isinstance(name_or_line, bool):
            return (
                f"Error: name_or_line must be a line number or name, "
                f"got {name_or_line!r}."
            )
        if isinstance(name_or_line, int):
            return index.read_function(path, name_or_line)
        if isinstance(name_or_line, str) and name_or_line.strip().isdigit():
            return index.read_function(path, int(name_or_line.strip()))
        if isinstance(name_or_line, str):
            return index.read_function_by_name(path, name_or_line)
        return (
            f"Error: name_or_line must be a line number or name, "
            f"got {name_or_line!r}."
        )
    except Exception as exc:
        return (
            f"Error: could not read_function {path!r} ({name_or_line!r}): "
            f"{type(exc).__name__}: {exc}"
        )
```

Return:
`[read_file, read_lines, read_function, list_files, search_codebase, find_function_at_line]`

Update `_build_tools` docstring to six tools. Update side-effect docstring if it still says five shared tools.

- [ ] **Step 4: Run suites**

```bash
cd backend && /Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_function_boundaries.py \
  agents/software_engineering_team/tests/test_false_positive_filter.py \
  agents/software_engineering_team/tests/test_side_effect_impact_pass.py \
  -q --tb=short
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add \
  backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_false_positive_filter.py \
  backend/agents/software_engineering_team/tests/test_side_effect_impact_pass.py
git commit -m "$(cat <<'EOF'
Expose read_function name lookup and tool dispatch.

Add read_function_by_name with ambiguous/missing errors and a unified strands tool.
EOF
)"
```

---

## Spec coverage self-review

| Spec requirement | Task |
|---|---|
| `iter_constructs` | Task 1 |
| `read_function_by_name` unique/missing/ambiguous | Task 2 |
| Shared success format | Task 2 |
| Unified tool dispatch | Task 2 |
| Unpack / side-effect tool-set updates | Task 2 |
| Prompts / find_references / line-semantics rewrite | Out of scope |
