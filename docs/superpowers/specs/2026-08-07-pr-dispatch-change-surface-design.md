# Design: PR reviewer dispatch — prefer change surface as primary code=

Date: 2026-08-07

## Goal

When admission produced a non-empty change surface, make that surface the
primary reviewer input (`code=` + `pre_numbered=True`) instead of whole-file
`head_files`, while keeping hunk `code` for files that still need it (e.g.
missing head).

## Context

Admission already attaches `ReviewModeDecision.change_surface` (head-backed
patches via the shared builder). `_run_reviewer` still prefers whole-file
`files=head_files` (`pre_numbered=False`) and only uses annotated hunk `code`
when fetch fails. Comment anchoring continues to use admission
`valid_by_path` / `changed_by_path`.

## Decisions

| Topic | Choice |
|---|---|
| Non-empty surface vs whole-file | Surface **replaces** the whole-file `files=` attempt |
| Focus note | Reuse `_hunk_review_focus` (surface is pre-numbered) |
| Partial fetch | Surface attempt + existing hunk `code` attempt when both non-empty; merge as today |
| Empty surface | Keep current `head_files` / `code` behavior |
| Wiring | Pass `change_surface` into `_run_reviewer`; do not overwrite `mode.code` |

## Scope

### In scope

- Add `change_surface` parameter to `_run_reviewer`
- Attempt-building rules above
- Pass `mode.change_surface` from `_run_pr_review_body`
- Offline unit tests asserting primary kwargs (and surface+hunk / empty-surface cases)

### Out of scope

- Whole-file fallback-only policy (when to retry whole-file after surface)
- Focus-note unification / new focus helper
- Builder / admission surface construction changes
- SE path

## Contract

### `_run_reviewer` (delta)

New parameter:

```python
change_surface: Optional[ChangeSurface] = None,
```

Attempt construction:

```text
if change_surface and not change_surface.is_empty:
    attempts += [{code: surface.code, pre_numbered: True,
                  task_requirements: _hunk_review_focus(...)}]
    # do NOT add files=head_files attempt
elif head_files:
    attempts += [{files: head_files, pre_numbered: False,
                  task_requirements: _whole_file_focus(...)}]

if code:  # existing hunk blob from admission
    attempts += [{code: code, pre_numbered: True,
                  task_requirements: _hunk_review_focus(...)}]
```

Preconditions / postconditions: update docstring to describe surface-primary
behavior; failure/merge semantics unchanged.

### Caller

```python
output = _run_reviewer(
    ...,
    mode.code,
    head_files=mode.head_files or None,
    change_surface=mode.change_surface,
    repo_reader=mode.repo_reader,
)
```

Anchoring: still `mode.valid_by_path` / `mode.changed_by_path` — unchanged.

## Data flow

```text
mode = _decide_review_mode(...)
        │
        ├─ change_surface (may be empty)
        ├─ head_files
        └─ code (hunk fallback subset)
                │
                ▼
_run_reviewer(...)
   non-empty surface? ──yes──► surface code= attempt (no files=)
         │                      + optional hunk code= if mode.code
         no
         ▼
   existing head_files / code attempts
```

## Testing

Offline, extend `TestRunReviewerUnit` (or equivalent):

1. Non-empty surface, no hunk `code` → one call: `code==surface.code`,
   `pre_numbered=True`, no `files` key (or `files` absent/falsy).
2. Empty surface + `head_files` → one whole-file call: `pre_numbered=False`.
3. Non-empty surface + non-empty hunk `code` → two calls, both
   `pre_numbered=True`; first uses surface code; no whole-file `files=` call.

Do not require live GitHub.

## Non-goals / YAGNI

- Concatenating surface + hunk into a single `code=` string
- New focus-note helper
- Changing partition / comment-posting paths beyond kwargs wiring
