# Design: Pair surface — emit change surface via shared patch path

Date: 2026-08-07

## Goal

Wire SE-style old/new content maps into the existing patch-surface emission
path so callers get the same pre-numbered expanded change surface as PR
patches, without duplicating expansion or formatting logic.

## Context

`unified_diffs_from_pairs` already turns path→new (and optional path→old)
maps into per-path unified-diff text (empty string when identical).
`build_change_surface_from_patches` already expands, merges, pre-numbers, and
emits `### path ###` blocks. `build_change_surface_from_pairs` remains a stub
that returns empty for `{}` and otherwise raises `NotImplementedError`.

This leaf replaces that stub with a thin composition of the two helpers.

## Decisions

| Topic | Choice |
|---|---|
| Public API | Keep `build_change_surface_from_pairs(new_contents, old_contents=None)` |
| Implementation | Thin compose (Approach A): diffs → `build_change_surface_from_patches` |
| Blank / identical diffs | Pass through; patch path already omits blank patches |
| Empty `new_contents` | Keep early `_empty_surface()` (equivalent to compose) |
| Format-parity tests | Golden: pairs surface equals patch path on the same derived diffs |
| Test coverage | Parent-complete: modified, new-file, identical, empty map |

## Scope

### In scope

- Implement `build_change_surface_from_pairs` via composition
- Update DbC postconditions to match real behavior (no stub /
  `NotImplementedError` wording)
- Replace pair stub tests with parent-complete unit coverage, including
  modified-file golden parity against the patch entry point

### Out of scope

- Resolving old/new content from git or disk
- PR patch admission / collapse policy changes
- Changing `unified_diffs_from_pairs` or patch-path internals
- Reimplementing expand / merge / pre-number / `### path ###` emission

## Public contract

### `build_change_surface_from_pairs`

Signature (unchanged):

```python
def build_change_surface_from_pairs(
    new_contents: Mapping[str, str],
    old_contents: Optional[Mapping[str, str]] = None,
) -> ChangeSurface:
```

Implementation shape:

```python
if not new_contents:
    return _empty_surface()
patches = unified_diffs_from_pairs(new_contents, old_contents)
return build_change_surface_from_patches(patches, new_contents=new_contents)
```

Preconditions:

- `new_contents` maps path → new-file content (may be empty).
- `old_contents`, when omitted/`None`, means empty old for every path.
  When provided, missing keys are treated as empty old for that path
  (same as `unified_diffs_from_pairs`).

Postconditions:

- `new_contents == {}` → empty `ChangeSurface` regardless of `old_contents`.
- Otherwise equivalent to
  `build_change_surface_from_patches(unified_diffs_from_pairs(...),
  new_contents=new_contents)`:
  - identical old/new → blank patch → path omitted
  - all paths identical / no assemblable bodies → empty surface
  - new files and modified files with assemblable bodies → same blocks/
    `code` as the patch path for those diffs
- Never raises for well-typed string mappings.

## Data flow

```text
new_contents (+ optional old_contents)
        │
        ▼
unified_diffs_from_pairs  →  dict[path, patch]  ("" if identical)
        │
        ▼
build_change_surface_from_patches(patches, new_contents=...)
        │
        ▼
ChangeSurface  (same format as PR patch path)
```

## Testing

Prefer extending `test_change_surface_api.py` (existing pairs stub tests live
there) or a focused `test_build_change_surface_from_pairs.py` if that stays
clearer. Required cases:

1. **Modified file (golden parity):** Build `patches = unified_diffs_from_pairs(...)`;
   assert `build_change_surface_from_pairs(...)` equals
   `build_change_surface_from_patches(patches, new_contents=...)`.
2. **New file** (`old_contents=None` and/or missing key): non-empty surface.
3. **Identical old/new:** empty surface.
4. **Empty `new_contents`:** empty surface (including when `old_contents` is
   non-empty).

Remove tests that expect `NotImplementedError` from the pairs stub.

## Non-goals / YAGNI

- Filtering blank diffs before calling the patch path
- A second public emit API name
- Asserting exact surface strings against hand-written unified diffs
  (parity against the patch entry point is enough)
