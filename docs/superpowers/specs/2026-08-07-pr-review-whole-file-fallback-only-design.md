# Design: PR review whole-file fallback-only partition

Date: 2026-08-07

## Goal

Make whole-file review a per-file degradation path after change-surface
admission, without skipping HTTP head fetch needed for surface expansion and
without silently dropping reviewable files when the surface covers only a
subset of the PR.

## Context

Admission already fetches head content for every reviewable changed file,
builds a change surface via the shared builder (`new_contents=head text`), and
dispatches a non-empty surface as the primary pre-numbered reviewer input.

Two gaps remain:

1. **`ReviewModeDecision.head_files` still holds every fetched path.** When the
   surface is non-empty, `_run_reviewer` uses `elif head_files`, so the
   whole-file attempt is skipped for the entire PR. Any fetched path the
   builder omitted never reaches a reviewer call.
2. **Hunk `code` is still keyed only to fetch failure**, not to “uncovered by
   surface.” That is fine when surface ⊆ fetched and omitted paths would have
   used whole-file — but only if whole-file still runs for those paths.

Desired outcome: fetch stays universal for expansion; decision `head_files`
means “whole-file reviewer inputs only”; surface + fallback whole-file + hunks
are path-disjoint and cover every reviewable file.

## Decisions

| Topic | Choice |
|---|---|
| Head HTTP fetch | Still fetch all reviewable files (required for surface expansion) |
| Decision `head_files` meaning | Paths for the whole-file reviewer attempt only (exclude surface paths) |
| Mixed surface + whole-file | Two independent reviewer attempts, then merge (existing multi-attempt pattern) |
| Hunk `code` | Only paths in neither surface nor fallback `head_files` (typically fetch failures) |
| `files_reviewed` | Count each reviewable path once across surface ∪ fallback ∪ hunks |
| API shape | Keep `ReviewModeDecision` fields; reinterpret `head_files` (no new fields) |
| Focus-note copy | Unchanged (owned by a sibling leaf) |
| Builder / SE path | Unchanged |

## Scope

### In scope

- Partition logic in `_decide_review_mode` after fetch + surface build
- Flip surface vs whole-file in `_run_reviewer` from exclusive (`elif`) to
  independent (`if` / `if`)
- Docstring / contract updates for `_decide_review_mode`, `_run_reviewer`, and
  `_MergedReviewerOutput` (path-disjoint sources when admission partitions)
- Unit tests for fallback-only triggering and mixed-mode dispatch

### Out of scope

- Change-surface builder implementation
- Diff-first focus-note unification / `pre_existing` copy rewrite
- SE pipeline surface derivation
- Broad suite rewrite of older whole-file-preference assertions (sibling leaf)

## Architecture

### Admission partition (`_decide_review_mode`)

```
reviewable gate (unchanged)
  └─ fetched = _fetch_head_files(...)          # all reviewable; expansion fuel
  └─ change_surface = _build_change_surface_for_reviewable(files, fetched)
  └─ surface_paths = set(change_surface.blocks)
  └─ head_files = {p: t for p, t in fetched.items() if p not in surface_paths}
  └─ uncovered = reviewable - surface_paths - set(head_files)
  └─ code = _build_review_code(files whose filename in uncovered)
       ("" when uncovered empty; total-fetch-fail / empty-hunk noop unchanged)
  └─ files_reviewed = |surface_paths ∪ head_files ∪ hunk_paths|
  └─ ReviewModeDecision(..., head_files=head_files, change_surface=..., code=...)
```

**Invariant (pairwise disjoint):** `surface_paths ∩ set(head_files) = ∅`,
`surface_paths ∩ hunk_paths = ∅`, and `set(head_files) ∩ hunk_paths = ∅`
(no path in more than one reviewer source).

### Dispatch (`_run_reviewer`)

```
attempts = []
if change_surface non-empty:  append surface attempt (pre_numbered, hunk focus)
if head_files:                append whole-file attempt (files=, whole-file focus)
if code:                      append hunk attempt (pre_numbered, hunk focus)
run attempts; merge via _MergedReviewerOutput
```

Surface is primary for **covered paths**, not a PR-wide suppressor of
whole-file. Admission is responsible for filtering `head_files` so the
whole-file attempt never re-reviews surface paths.

### Coverage matrix

| Situation | `change_surface` | decision `head_files` | `code` |
|---|---|---|---|
| All reviewable expand into surface | non-empty | `{}` | `""` |
| Some expand; some fetched but builder-omitted | partial | omitted paths only | `""` |
| Some expand; some fetch fail | partial | `{}` | hunks for missing |
| Surface empty; all fetch | empty | all fetched | `""` |
| Surface empty; partial fetch | empty | fetched | hunks for missing |
| Total fetch fail | empty | `{}` | all hunks (noop if blank) |

## Error handling

- Fetch failures remain soft inside `_fetch_head_files` (unchanged).
- Empty surface + empty fallback head + empty hunk `code` still hits the
  existing noop completion path.
- Reviewer attempt failure stays all-or-nothing per `_run_reviewer`
  (unchanged): one failed attempt records outage and returns `None`.

## Testing

- **Admission (mocked fetch/builder):** when surface includes path A and omits
  fetched path B, decision `head_files` is `{B: ...}` only; when surface covers
  every fetched path and none missing, `head_files == {}` and `code == ""`;
  fetch-missing path C appears only in hunk `code`; `files_reviewed` equals
  unique covered paths.
- **`_run_reviewer`:** surface + non-empty filtered `head_files` → two attempts
  (surface then whole-file), merged; surface only → no `files=` call; whole-file
  attempt’s `files` keys never intersect `change_surface.blocks`.

## Risks

- Callers or tests that assumed decision `head_files` == “all successfully
  fetched content” will need updates (expected; sibling leaf may broaden this).
- Double-review if someone later passes unfiltered `head_files` into
  `_run_reviewer` alongside a surface — admission contract must stay the single
  partition point (documented; optional defensive subtract in dispatch is not
  required for v1).
