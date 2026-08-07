# Design: Pair surface — unified diffs from old/new content maps

Date: 2026-08-07

## Goal

Provide a public helper that turns SE-style path→new (and optional path→old)
content maps into per-path unified-diff text suitable for later patch-surface
assembly, without emitting the expanded change surface in this leaf.

## Context

The patch path already accepts GitHub-style unified diffs via
`build_change_surface_from_patches`. The pairs path needs an intermediate step:
derive those diffs from in-memory old/new strings (no git/disk).

`difflib.unified_diff` is sufficient: `extract_touched_lines` /
`parse_valid_lines` ignore lines before the first `@@` hunk header, so
`--- a/<path>` / `+++ b/<path>` headers are fine.

## Decisions

| Topic | Choice |
|---|---|
| Public API | `unified_diffs_from_pairs(new_contents, old_contents=None) -> dict[str, str]` in `change_surface.py` |
| Identical old/new | Keep the path key with value `""` |
| New file | Missing key in `old_contents`, or `old_contents is None` → old = `""` |
| Diff shape | Full `difflib.unified_diff` with `--- a/<path>` / `+++ b/<path>` |
| Implementation | Thin stdlib wrapper (Approach A) |
| Deletes | Paths only in `old_contents` are ignored (iteration driven by `new_contents`) |

## Scope

### In scope

- Implement and export `unified_diffs_from_pairs`
- DbC docstring (preconditions / postconditions)
- Unit tests: new file, modified file, identical → empty string
- Assert produced non-empty diffs are consumable by `extract_touched_lines`
  (at least one added line for new/modified cases)

### Out of scope

- Calling `build_change_surface_from_patches` / expansion / `### path ###`
  emission (follow-on emit leaf)
- Resolving old/new content from git or disk
- PR patch parsing
- Changing `build_change_surface_from_pairs` stub (still emit leaf)

## Public contract

### `unified_diffs_from_pairs`

Signature:

```python
def unified_diffs_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
```

Preconditions:

- `new_contents` maps path → new-file text (may be empty).
- `old_contents`, when omitted/`None`, means empty old for every path.
  When provided, missing keys are treated as empty old for that path.

Postconditions:

- `new_contents == {}` → `{}`.
- Result contains exactly the keys of `new_contents`, in iteration order
  (implementation may use `dict`, which preserves insertion order on
  supported Python).
- For each path: if old text equals new text → value `""`; otherwise a
  non-empty unified diff string from `difflib.unified_diff` with
  `fromfile=f"a/{path}"`, `tofile=f"b/{path}"`, using
  `splitlines(keepends=True)`.
- Never raises for well-typed string mappings.

## Data flow

```text
new_contents (+ optional old_contents)
        │
        ▼
per path: resolve old ("" if missing/None map)
        │
        ├─ old == new ──► ""
        └─ else ──► difflib.unified_diff → patch text
        │
        ▼
dict[path, patch]   (for later build_change_surface_from_patches)
```

## Testing

- New module or extend change-surface tests:
  `test_unified_diffs_from_pairs.py` (preferred focused file).
- Cases:
  - New file (`old_contents=None` and/or missing key) → non-empty diff with `+`
    lines; `extract_touched_lines` non-empty
  - Modified file → non-empty diff; touched lines include the added line
  - Identical old/new → `""`
  - Empty `new_contents` → `{}`

## Non-goals / YAGNI

- Custom hunk formatting beyond `difflib`
- Omitting identical keys from the result dict
- Binary / non-text content handling
