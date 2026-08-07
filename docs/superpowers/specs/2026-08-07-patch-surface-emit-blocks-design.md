# Design: Patch surface — emit pre-numbered path blocks with expansion

Date: 2026-08-07

## Goal

Implement the PR/unified-patch assembly path so `build_change_surface_from_patches`
returns a chunker-ready `ChangeSurface`: per-path bodies are pre-numbered
(`N: `) slices of new-file content expanded around added-only touched lines,
wrapped as `### path ###` blocks via the existing formatter.

## Context

Diff-first change surface already provides:

- `extract_touched_lines(patch)` — added (`+`) new-file lines only
- `expand_touched_ranges(content, touched, path=...)` — AST / capped fallback
  ranges
- `format_change_surface_code(blocks)` — `### path ###` join
- Stub `build_change_surface_from_patches` — empty/blank → empty surface;
  otherwise `NotImplementedError`

Parse/annotate helpers (`render_patch_hunks`) stay available for other callers;
this leaf uses the patch only to derive touched lines, then expands against
full new-file content.

## Decisions

| Topic | Choice |
|---|---|
| Body source | Expanded slices from `new_contents` (not annotated hunk text) |
| Missing / blank `new_contents` for a path | **Omit** that path |
| Empty touched set (no `+` lines) | **Omit** that path |
| Multiple ranges | Merge overlapping/adjacent ranges, then join remaining gaps with `...` |
| Structure | Private per-path assembler + public `build_change_surface_from_patches` (Approach B) |
| Path order | Insertion order of `patches` (surviving paths only) |

## Scope

### In scope

- Implement `build_change_surface_from_patches(patches, *, new_contents=None)`
- Private helpers (names illustrative):
  - `_merge_line_ranges(ranges) -> list[LineRange]` — sort, merge overlap /
    adjacency (`next.start <= prev.end + 1`)
  - `_pre_number_ranges(content, ranges) -> str` — slice 1-based lines, prefix
    `N: `, insert a bare `...` line between non-merged gaps
  - `_assemble_path_block(path, patch, content) -> str | None` — touched →
    expand → merge → pre-number; `None` when omitted
- Update module / stub docstrings to describe real postconditions
- Tests: single-file emit with expansion; multi-file; omit without content;
  omit without added lines; replace the non-empty `NotImplementedError`
  expectation with a successful assembly case

### Out of scope

- Old/new pair assembly (`build_change_surface_from_pairs`)
- Same-construct multi-hunk collapse beyond adjacent/overlap merge (sibling
  collapse work)
- PR / SE admission flips
- Changing `extract_touched_lines` / `expand_touched_ranges` contracts
- Using `render_patch_hunks` as the emitted body

## Public contract

### `build_change_surface_from_patches`

Preconditions:

- `patches` maps path → one file’s unified-diff text (GitHub `files[].patch`
  style). May be empty.
- `new_contents`, when provided, maps path → full new-file text used for
  expansion. Omitted/`None` means no content for any path.

Postconditions:

- `patches == {}` or every patch value blank → empty `ChangeSurface`.
- For each path with a non-blank patch, in iteration order:
  - Missing / blank `new_contents[path]` → omit path.
  - `extract_touched_lines(patch)` empty → omit path.
  - Otherwise expand with `expand_touched_ranges(content, touched, path=path)`,
    merge ranges, pre-number; omit if the resulting body is empty.
- Returned `ChangeSurface.blocks` contains only surviving paths; `.code`
  equals `format_change_surface_code(blocks)`.
- Never raises for well-typed string mappings (no `NotImplementedError`).

## Per-path data flow

```text
patch + new_contents[path]
        │
        ├─ blank content? ──► omit
        ├─ extract_touched_lines ── empty? ──► omit
        │
        ▼
expand_touched_ranges(content, touched, path)
        │
        ▼
merge overlapping/adjacent LineRanges
        │
        ▼
pre-number slices (N: line) ; "..." between gaps
        │
        ▼
blocks[path] = body  →  format_change_surface_code
```

## Merge / pre-number rules

- Sort ranges by `(start_line, end_line)`.
- Merge while `next.start_line <= current.end_line + 1`.
- For each merged range, emit lines `start_line..end_line` from
  `content.splitlines()` as `f"{n}: {line}"` (1-based `n`).
- Between successive merged ranges, emit a single line `...` (no leading
  spaces), matching the annotated-hunk gap marker convention.
- If `end_line` exceeds file length, clamp to the last line (defensive; should
  not occur when ranges come from `expand_touched_ranges` on the same content).

## Error handling

- No network / LLM.
- Malformed patches that yield no added lines → omit (not an error).
- Do not fall back to annotated hunks when content is missing.

## Testing

- Focused tests under
  `backend/agents/software_engineering_team/tests/` (extend
  `test_change_surface_api.py` and/or add
  `test_change_surface_from_patches.py`).
- Cover multi-file input (acceptance criterion).
- Pure unit tests only.

## Non-goals / YAGNI

- Public `assemble_expanded_block` API
- Configurable gap marker
- Whole-file fallback when expansion returns empty for a path with touched
  lines (treat as omit; expansion helper already returns capped ranges for
  typical cases)
