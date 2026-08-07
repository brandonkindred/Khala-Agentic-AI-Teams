# Design: Code-review `read_function` by name + tool wrapper

Date: 2026-08-07

## Goal

Add name-based construct lookup (`read_function_by_name`) and expose a
strands `read_function` tool that dispatches to line or name resolution, with
clear errors for missing and ambiguous names.

## Context

Line-based `CodebaseIndex.read_function(path, line)` already exists. Parent
scoped-tools work wants agents to load a single function/class body by name
or line. This leaf adds name resolution and the tool-facing wrapper; prompts
and `find_references` stay out of scope.

## Decisions

| Topic | Choice |
|---|---|
| Index API | Keep `read_function(path, line)`; add `read_function_by_name(path, name)` |
| Name match | Exact, case-sensitive on `EnclosingConstruct.name` (bare or `Class.method`) |
| Construct listing | New `iter_constructs(content)` in `function_boundaries` |
| Success format | Same header + `N\| content` as line-based reads (shared formatter) |
| Tool | Single `read_function(path, name_or_line)` that dispatches int/digits → line, else name |
| Ambiguity | Explicit error listing matches |
| Non-Python | Clear error (same Python-only policy as line path) |

## API & behavior

### `iter_constructs(content) -> list[EnclosingConstruct]`

AST walk over `FunctionDef` / `AsyncFunctionDef` / `ClassDef`. Naming and
ranges match `_enclosing_construct_ast` (methods qualified as
`ClassName.method`; decorator-aware start). Returns `[]` on parse failure;
never raises.

### `CodebaseIndex.read_function_by_name(path, name) -> str`

1. Reject blank / non-str `name`.
2. `_read(path)`; reuse path errors.
3. Require `.py` / `.pyi`.
4. Strip annotated prefixes if present; `iter_constructs(stripped)`.
5. Filter `c.name == name` (exact).
6. 0 → missing error; 2+ → ambiguous error; 1 → format via shared helper.

### Shared formatter

Private helper used by line and name paths:
`{path} {kind} {name} lines {start}–{end} ({n} lines):` + `N| content`.

### Tool `read_function(path, name_or_line)`

Registered in `_build_tools` after `read_lines`. Dispatch:

- `int` (not bool) → `index.read_function(path, line)`
- `str` of digits only → parse int, then line path
- otherwise string → `index.read_function_by_name(path, name)`
- other types → `Error: name_or_line must be a line number or name, got ...`

Never raises; unexpected exceptions become `Error: ...`.

Tool return order becomes six tools:
`[read_file, read_lines, read_function, list_files, search_codebase, find_function_at_line]`.

### Error strings (stable)

| Case | Message shape |
|---|---|
| Bad / blank name | `Error: name must be a non-empty string, got ...` |
| Missing | `Error: no function/class named {name!r} in PATH.` |
| Ambiguous | `Error: name {name!r} is ambiguous in PATH; matches: a (lines S–E), b (lines S–E).` |
| Non-Python | Same style as line path (requires `.py`/`.pyi`) |
| Bad tool arg | `Error: name_or_line must be a line number or name, got ...` |

## Testing

- Unique qualified name returns construct body matching line-based read
- Missing name → error
- Ambiguous name → error listing matches
- Tool digit/`int` vs name dispatch; never-raise on index boom
- Update `_build_tools` 5-tuple unpacks → 6-tuples
- Side-effect exact tool-set assertion includes `read_function`

## Files

| File | Change |
|---|---|
| `function_boundaries.py` | `iter_constructs` |
| `false_positive_filter.py` | `read_function_by_name`, shared formatter, tool, docstring |
| `test_false_positive_filter.py` | Name + tool tests; unpack updates |
| `test_side_effect_impact_pass.py` | Tool-set assertion |
| `tests` for `iter_constructs` | If a dedicated boundaries test module exists; else cover via index tests |

## Out of scope

- Changing line-based resolution semantics
- `find_references` / excerpts
- Prompt rewrites
- Non-Python heuristic name lookup
