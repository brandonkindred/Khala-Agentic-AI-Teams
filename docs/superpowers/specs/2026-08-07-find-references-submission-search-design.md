# Design: find_references submission search (path:line hits)

Date: 2026-08-07

## Goal

Add a `CodebaseIndex.find_references` API that searches the current
submission/index for symbol substring hits and returns capped `path:line`
results (with a clear empty message when none), so in-submission references are
discoverable without opening whole files.

## Context

Diff-first code review is adding scoped inspection tools
(`read_lines`, `read_function`, `find_references`). Submission search is the
first slice of the `find_references` search half:

- Repo-reader search with caps — sibling work
- Truncation signaling / no-reader behavior — sibling work
- Enclosing-construct excerpts — sibling work
- Tool wiring into `_build_tools` / prompts — sibling work

Today `CodebaseIndex.search` already performs case-insensitive substring search
over in-memory sources (submission files + existing-codebase excerpt), capped by
`_SEARCH_MATCH_LIMIT` (60), returning `(path, lineno, line_text)` tuples. The
`search_codebase` tool formats those as `path:line: text`. This leaf reuses that
scanner and exposes a string API shaped for the eventual `find_references` tool.

## Decisions

| Topic | Choice |
|---|---|
| Approach | Thin wrapper over `CodebaseIndex.search` |
| Hit format | `path:line` only (no line text) |
| Empty result | `No references for {symbol!r}.` |
| Corpus | Same as `search`: submission files + existing-codebase excerpt |
| Cap | Default `_SEARCH_MATCH_LIMIT`; silent truncate for now (truncation flag is sibling) |
| Match semantics | Inherited from `search` (case-insensitive substring; blank → no hits) |
| Repo reader | Out of scope (sibling) |
| Tool / prompts | Out of scope (sibling wiring) |
| Excerpts | Out of scope (sibling enrichment) |

## API

```python
def find_references(self, symbol: str, max_matches: int = _SEARCH_MATCH_LIMIT) -> str:
```

### Preconditions

- `max_matches` > 0 (else raise `ValueError`, matching `search`)

### Postconditions

- On hits: newline-joined `path:line` strings for the first `max_matches`
  occurrences in path-then-line order (same order as `search`).
- On no hits (including blank/whitespace-only `symbol` after `search`'s strip):
  return `No references for {symbol!r}.`
- Never consults `repo_reader`.
- Never attaches excerpts or truncation banners.

## Placement

- Method on `CodebaseIndex` in
  `backend/agents/software_engineering_team/code_review_agent/false_positive_filter.py`
- Immediately after `search` (same module as the other scoped index APIs)

## Testing

Unit tests in `test_false_positive_filter.py` using in-memory fixture files:

1. Multi-file hits → capped `path:line` lines; no line text; excerpt included when present
2. Unknown symbol → empty message
3. Whitespace-only symbol → empty message
4. Cap → exactly `max_matches` lines when more exist
5. `max_matches <= 0` → `ValueError`

## Out of scope

- Repo-wide / `repo_reader` search
- Truncation signaling
- Excerpt attachment
- `_build_tools` / prompt changes
- Replacing or removing `search_codebase`
