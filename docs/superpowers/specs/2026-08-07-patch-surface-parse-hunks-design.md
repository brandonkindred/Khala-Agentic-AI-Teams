# Design: Patch surface — parse unified/PR patches into touched lines and hunks

Date: 2026-08-07

## Goal

Expose a change-surface entry point that turns one file’s unified / PR patch
text into (1) the set of **touched** new-file line numbers and (2) annotated
hunk text ready for later expansion and `### path ###` assembly.

## Context

Diff-first review already has pure helpers in
`github_source/pr_review_mapping.py`:

- `parse_valid_lines(patch, *, added_only=...)` — new-file line numbers from a
  unified diff
- `render_annotated_hunks(patch)` — added + context lines prefixed with
  `N: ` and `...` between non-contiguous hunks

`code_review_agent/change_surface.py` already owns `expand_touched_ranges` and
stubs `build_change_surface_from_patches`. This leaf only wires parse + annotate
into the change-surface module; it does **not** emit the final surface.

## Decisions

| Topic | Choice |
|---|---|
| Touched lines | **Added (`+`) only** — `parse_valid_lines(patch, added_only=True)` |
| Annotated hunks | Reuse `render_annotated_hunks` unchanged (still includes context for display) |
| API shape | Two thin wrappers in `change_surface.py` (Approach A) |
| Placement | Same module as expansion / surface types so the later assembly path stays local |
| Multi-file map helper | Out of scope — compose per path in the follow-on emit step |
| `build_change_surface_from_patches` | Remains stub / `NotImplementedError` until the emit leaf |

Rationale for added-only touched set: expansion already grows to enclosing
constructs or a capped window, so starting from real edits keeps the touched
set small and meaningful. Annotated text still shows context so reviewers (and
later surface emission) see hunk neighborhood.

## Scope

### In scope

- Add to `change_surface.py` and `__all__`:
  - `extract_touched_lines(patch: str) -> frozenset[int]`
  - `render_patch_hunks(patch: str) -> str`
- Implement as thin wrappers around existing `github_source` helpers (import
  from `github_source.pr_review_mapping` or the package re-export — prefer the
  same import path already used by PR review callers when practical).
- DbC docstrings (preconditions / postconditions) on both public functions.
- Focused unit tests for a **single-file** unified patch:
  - Added lines appear in the touched set; context and removed lines do not
  - Empty / blank patch → empty frozenset and `""`
  - `render_patch_hunks` output equals `render_annotated_hunks` for the same
    input (wrapper fidelity)

### Out of scope

- Emitting pre-numbered `### path ###` blocks or calling `expand_touched_ranges`
  in this leaf
- Old/new content-pair path
- PR / SE admission flips
- Multi-file batch helper (callers loop over paths)

## Public contract

### `extract_touched_lines(patch: str) -> frozenset[int]`

Preconditions:

- `patch` is one file’s unified-diff text (GitHub `files[].patch` style), or
  empty / blank for binary / oversized / unchanged files.

Postconditions:

- Returns the frozenset of 1-based new-file line numbers that appear as added
  (`+`) lines in the patch.
- Context (` `), removed (`-`), and `\ No newline at end of file` markers are
  never included.
- Empty or blank `patch` → empty frozenset.
- Never raises.

### `render_patch_hunks(patch: str) -> str`

Preconditions:

- Same as `extract_touched_lines`.

Postconditions:

- Identical to `render_annotated_hunks(patch)` for every input (byte-for-byte /
  string equality).
- Empty or blank `patch` → `""`.
- Never raises.

## Data flow

```text
unified patch (one file)
        │
        ├─► extract_touched_lines ──► frozenset[int]   (added only)
        │         (for expand_touched_ranges later)
        │
        └─► render_patch_hunks ─────► annotated text
                  (reuse render_annotated_hunks)
```

Follow-on emit leaf will take path + new content + these outputs, expand
touched lines, and assemble `### path ###` blocks. That work is not part of
this design.

## Error handling

No new failure modes: empty patches are valid no-ops; malformed hunk headers
that existing parsers skip remain skipped. Do not introduce network or LLM
dependencies.

## Testing

- New focused test module under
  `backend/agents/software_engineering_team/tests/` (e.g.
  `test_change_surface_patch_parse.py`).
- Pure unit tests only — no network, no LLM, no GitHub client.
- Cover at least one realistic single-file hunk with `+`, ` `, and `-` lines so
  touched-set vs annotated-text divergence is explicit and locked.

## Non-goals / YAGNI

- Do not reimplement hunk parsing in `change_surface.py`.
- Do not change `COMMENT_ON_ADDED_LINES_ONLY` or the default of
  `parse_valid_lines` for PR comment mapping — only the change-surface wrapper
  passes `added_only=True`.
- Do not add a combined `ParsedPatchHunks` dataclass unless a follow-on leaf
  proves one call site needs it.
