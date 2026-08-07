# Design: Code-review `read_function` by line number

Date: 2026-08-07

## Goal

Add `CodebaseIndex.read_function(path, line)` that resolves the enclosing
Python function/method/class via `function_boundaries.enclosing_construct`
and returns only that construct's body (with a minimal header), or a clear
`Error: ...` string when unresolved.

## Context

Diff-first code review is adding scoped inspection tools. `read_lines` already
exists on `CodebaseIndex` and in `_build_tools`. Full `read_function` (name or
line + tool wrapper) is split into leaf work: this change implements
**line-number resolution only**. Name lookup and the strands tool wrapper are
sibling work; pass wiring and prompt rewrites are out of scope.

## Decisions

| Topic | Choice |
|---|---|
| Placement | `CodebaseIndex.read_function(path, line: int) -> str` |
| Success format | Mirror `read_lines`: header + `N\| content` body |
| Header | `{path} {kind} {name} lines {start}–{end} ({n} lines):` |
| Lookup | `enclosing_construct` after optional annotated-hunk strip (same as `find_function_at_line`) |
| Non-Python / no construct | Clear `Error: ...` — no heuristic body in this leaf |
| Name resolution | Deferred (sibling) |
| Tool / pass wiring | Deferred (sibling / later) |
| Span cap | Not applied — construct bodies are not limited by `_READ_LINES_MAX_SPAN` |

## API & validation

### Signature

```python
def read_function(self, path: str, line: int) -> str: ...
```

### Validation / resolution order

1. Reject non-positive or non-int `line` (including bools).
2. Resolve and read `path` via `_read` (same as `read_file` / `read_lines`).
3. If extension is not `.py` / `.pyi` → Python-only error.
4. Strip annotated hunk prefixes when present; call
   `enclosing_construct(stripped, physical, annotated_hunks=mapper is not None)`.
5. If construct is `None` → unresolved error.
6. Slice inclusive `[start_line, end_line]` from the original (unnumbered) lines
   for display coordinates when a mapper is present; emit header + numbered body.

### Success format

```
{path} {kind} {name} lines {start}–{end} ({n} lines):
{start}| {line}
...
{end}| {line}
```

`kind` / `name` come from `EnclosingConstruct` (`function` / `class`; methods
qualified as `ClassName.method`).

### Error strings (stable substrings)

| Case | Message shape |
|---|---|
| Bad `line` | `Error: line must be a positive integer, got ...` |
| Path miss / ambiguous | Reuse `_read` / `read_file` messages |
| Non-Python | `Error: read_function by line requires a Python file (.py/.pyi); got {path}.` |
| Unresolved | `Error: no enclosing function/class for line L of PATH.` |

## Testing

Extend `test_false_positive_filter.py`:

- Method-in-class: line inside a method returns that method body; header includes
  `function Class.method`
- Module-level / unresolved → clear error
- Non-Python path → clear error
- Bad `line` → clear error (never raises)

## Files

| File | Change |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | `CodebaseIndex.read_function` |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Contract tests |

## Out of scope

- Name-based resolution
- Strands `@tool` wrapper / `_build_tools` registration
- Prompt rewrites
- Pass-specific wiring beyond the index method
- Non-Python heuristic construct bodies
