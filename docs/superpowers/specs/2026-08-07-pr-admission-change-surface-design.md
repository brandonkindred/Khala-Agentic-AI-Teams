# Design: PR admission — build change surface for head-backed patches

Date: 2026-08-07

## Goal

During PR review admission (`_decide_review_mode`), build a change surface via
the shared builder for every reviewable file that has a usable patch **and**
successfully fetched head content, and attach it to the admission decision for
a later dispatch leaf. Do not change reviewer `code=` selection in this leaf.

## Context

Today `_decide_review_mode` fetches head contents for reviewable files
(non-removed + has patch), prefers whole-file review when fetch succeeds, and
falls back to annotated hunk `code` via `_build_review_code` when fetch fails.
`ReviewModeDecision` carries `head_files`, hunk `code`, line maps, and
`repo_reader` — but no change surface.

`build_change_surface_from_patches` already expands/merges/pre-numbers when
given patches plus `new_contents`. Expansion requires head text, so surface
build is gated on successful head fetch for that path.

## Decisions

| Topic | Choice |
|---|---|
| Storage | Extend `ReviewModeDecision` with required `change_surface: ChangeSurface` |
| Empty case | Always a `ChangeSurface` (empty `blocks` when nothing assemblable) |
| Eligibility | Head-backed only: reviewable + patch + key present in `head_files` |
| Unusable / no-head | Omit from surface; existing hunk/`code` fallback unchanged |
| Dispatch | Unchanged (`_run_reviewer` still uses current `code` / whole-file paths) |
| Structure | Small helper used by `_decide_review_mode` (easier offline unit tests) |

## Scope

### In scope

- Add `change_surface` to `ReviewModeDecision`
- Helper that selects head-backed reviewable patches and calls
  `build_change_surface_from_patches`
- Wire helper into `_decide_review_mode` after head fetch
- Offline unit tests: usable head-backed patch → non-empty surface path;
  missing head / unusable patch → empty or omitted; existing decision fields
  still behave

### Out of scope

- Preferring surface as primary `code=` / `pre_numbered=True` in `_run_reviewer`
  (follow-on dispatch leaf)
- Focus-note copy changes
- SE / pairs admission path
- Changing the change-surface builder itself

## Public / internal contract

### `ReviewModeDecision`

Add field (order may place it after `head_files` or before `code`):

```python
change_surface: ChangeSurface
```

Invariants:

- Always present (never `None`).
- Empty surface (`is_empty`) when no head-backed usable patches assemble a body.
- Does not replace `code` or `head_files` in this leaf.

### Helper (name illustrative)

```python
def _build_change_surface_for_reviewable(
    files: Sequence[Any],
    head_files: Mapping[str, str],
) -> ChangeSurface:
```

Preconditions:

- `files` is the PR changed-file list (may be empty).
- `head_files` maps path → non-blank head text for successfully fetched files.

Postconditions:

- Considers only files that pass `_is_whole_file_reviewable` and whose
  `filename` is in `head_files`.
- Builds `patches` from those files' `.patch` values and calls
  `build_change_surface_from_patches(patches, new_contents=head_files)`.
- Returns empty `ChangeSurface` when the candidate set is empty or the builder
  omits all paths.
- Never raises for well-typed inputs (builder contract).

### `_decide_review_mode`

After computing `head_files` (and existing `code` / `files_reviewed` logic),
set `change_surface = _build_change_surface_for_reviewable(files, head_files)`
on the returned `ReviewModeDecision`. Noop / `None` return paths unchanged.

## Data flow

```text
files (PR)
   │
   ├─ reviewable gate (unchanged)
   ├─ valid_by_path / changed_by_path (unchanged)
   ├─ head_files = _fetch_head_files(...)
   │
   ├─ code / files_reviewed (existing whole vs hunk logic)
   │
   └─ change_surface = builder(patches for head ∩ reviewable,
                               new_contents=head_files)
         │
         ▼
   ReviewModeDecision(..., change_surface=...)
```

## Testing

Offline only (mock GitHub client / inject `head_files` via helper tests):

1. One reviewable file with patch + head content → `change_surface` non-empty,
   path present, body/code shaped like builder output (`### path ###` or
   blocks key).
2. Reviewable file with patch but **no** head entry → surface empty (or path
   absent); hunk/`code` path still available as today when fetch fails.
3. Removed / no-patch file → not in surface.
4. Call sites constructing `ReviewModeDecision` updated for the new field
   (tests that build the NamedTuple).

Prefer testing the helper directly for (1)–(3); one admission-level test if
existing `_decide_review_mode` fixtures make it cheap.

## Non-goals / YAGNI

- Changing whole-file vs hunk preference in this leaf
- Building surface without head content
- Optional/`None` surface field
- Live GitHub integration tests
