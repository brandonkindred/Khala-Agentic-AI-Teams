# Design: find_references enclosing-construct excerpts per hit

Date: 2026-08-07

## Goal

For each `find_references` hit, attach the enclosing function/class excerpt via
boundary helpers when they resolve, so hits include caller construct context
without full file bodies.

## Context

`find_references` already returns capped `path:line` hits (submission + optional
repo_reader) with truncation / no-reader signaling. Parent work wants each hit
enriched with a bounded enclosing construct. This leaf attaches excerpts when
boundaries resolve; size cap and line-window fallback are sibling work.

`read_function` already resolves `.py`/`.pyi` content via `enclosing_construct`
and formats with `_format_construct_slice`. Reuse those helpers.

## Decisions

| Topic | Choice |
|---|---|
| Hit format when excerpt resolves | `path:line` then `read_function`-style construct slice |
| Hit format when unresolved | `path:line` only (no note) |
| Languages | Same as `read_function`: `.py` / `.pyi` only for excerpts |
| Implementation | Local enrich helper using `enclosing_construct` + `_format_construct_slice` |
| Size cap / line-window fallback | Out of scope (sibling) |
| Search / truncation / no-reader | Unchanged; banners still follow the hit body |

## Output shape

```text
path:line
<path> <kind> <name> lines <start>–<end> (<n> lines):
N| ...

path2:line2
...
```

Hit blocks separated by a blank line. Truncation / no-reader banners append after
the hit body as today.

## Enrichment algorithm

For each `(path, lineno, _)` hit:

1. Load content via `_read` (submission / excerpt / reader).
2. If extension is not `.py`/`.pyi`, or content unreadable, or
   `enclosing_construct` returns `None` → emit `path:line` only.
3. Else emit `path:line` + `_format_construct_slice(...)` (after
   `strip_numbered_prefixes` when needed, matching `read_function`).

## Testing

1. Python hit inside a function → excerpt includes construct name/body
2. Unresolved (module-level or non-Python) → `path:line` only
3. Existing find_references tests updated for multi-line hit blocks

## Out of scope

- Excerpt size cap
- Line-window fallback when no construct
- Tool / prompt wiring
- Changing hit discovery / caps / truncation semantics
