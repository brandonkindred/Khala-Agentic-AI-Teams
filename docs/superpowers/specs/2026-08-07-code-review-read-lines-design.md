# Design: Code-review `read_lines` with hard max span

Date: 2026-08-07

## Goal

Add `read_lines(path, start, end)` on the shared code-review tool surface so
agents can fetch a bounded inclusive line slice without loading whole files.
Enforce a hard maximum span and return clear `Error: ...` strings for invalid
or oversized ranges.

## Context

False-positive verification, architecture consistency, side-effect impact, and
the merged architecture/side-effect pass share tools built by
`_build_tools` in `false_positive_filter.py` (side-effect adds
`search_repository` on top). Today those tools expose full-file `read_file`
only. Diff-first review wants scoped inspection: bounded line slices first,
with construct-scoped and reference tools landing in sibling work.

This change implements only `read_lines` and exposes it via the shared builder.
Prompt rewrites, `read_function`, `find_references`, and pass-specific wiring
beyond inheriting `_build_tools` are out of scope.

## Decisions

| Topic | Choice |
|---|---|
| Placement | `CodebaseIndex.read_lines` + thin `@tool` in `_build_tools` |
| Max span | 400 inclusive lines (`_READ_LINES_MAX_SPAN = 400`) |
| Line numbering | 1-based inclusive `start` / `end` |
| Past-EOF `end` | Clamp to last line when `start` is in range |
| Past-EOF `start` | Explicit error (empty result) |
| Success format | Header with path/range/count, then `N\| content` body lines |
| Failure style | Never raise from the tool; return `Error: ...` like existing tools |
| Path resolution | Identical to `read_file` (exact, suffix, existing-codebase, repo reader) |
| Pass wiring | Automatic via `_build_tools` only |
| Prompts | Unchanged |

## API & validation

### Signature

```python
def read_lines(self, path: str, start: int, end: int) -> str: ...
```

Constant: `_READ_LINES_MAX_SPAN = 400`.

### Validation order

1. Reject non-positive or non-int `start`/`end` (including bools), matching
   `find_function_at_line`.
2. If `start > end` → inverted-range error.
3. If `(end - start + 1) > 400` → oversize error naming the maximum.
4. Resolve and read `path` via existing `_read` / `read_file_or_none` path
   semantics; missing or ambiguous paths return the same error style as
   `read_file`.
5. If `start` is beyond the file length → beyond-EOF error.
6. If `end` is beyond the file length → clamp `end` to `len(lines)`.
7. Return success payload for the inclusive slice.

### Success format

```
{path} lines {start}–{end_effective} ({n} lines):
{start}| {line}
...
{end_effective}| {line}
```

Header uses the resolved display path when available. Body uses a pipe
separator so content that starts with digits stays unambiguous.

### Error strings (stable substrings for tests)

| Case | Message shape |
|---|---|
| Inverted range | `Error: invalid range: start (S) > end (E).` |
| Oversize span | `Error: range spans N lines; maximum is 400. Narrow start/end or use read_function.` |
| `start` past EOF | `Error: start line S is beyond the end of PATH (file has L lines).` |
| Bad `start`/`end` type | `Error: start must be a positive integer, got ...` (and same for `end`) |
| Path miss / ambiguous | Reuse existing `_read` / `read_file` messages |

## Tool exposure

Register `read_lines` inside `_build_tools` next to `read_file`. Update the
`_build_tools` postcondition docstring from four tools to five
(`read_file`, `read_lines`, `list_files`, `search_codebase`,
`find_function_at_line`). Architecture / side-effect / merged passes that call
`_build_tools` or `build_side_effect_tools` (which spreads `_build_tools`)
receive the new tool without further wiring.

The tool wrapper catches unexpected exceptions and returns
`Error: could not read_lines ...` so a bad argument never aborts the agent loop.

## Testing

Extend `test_false_positive_filter.py`:

- Valid inclusive range → header + numbered body for only that slice
- Inverted range → error
- Span of 401 → oversize error mentioning 400
- Clamp when `end` exceeds file length
- `start` past EOF → error
- `_build_tools` includes `read_lines` and existing unpack sites that assume
  exactly four tools are updated

## Files

| File | Change |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py` | Constant, `CodebaseIndex.read_lines`, tool registration, docstring |
| `backend/agents/software_engineering_team/tests/test_false_positive_filter.py` | Contract tests + tool-count unpack updates |

## Out of scope

- `read_function` / `find_references`
- Prompt rewrites that prefer scoped reads
- Pass wiring beyond exposing the tool through `_build_tools`
- Env-var override for the max span (fixed constant for now)
