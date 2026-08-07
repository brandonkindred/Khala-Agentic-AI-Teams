# Design: Unify diff-first PR review focus note

Date: 2026-08-07

## Goal

Replace `_whole_file_focus` / `_hunk_review_focus` in `api/pr_review.py` with a
single `_diff_first_focus` helper so every PR reviewer attempt shares one
change-scoped focus note: diff-first framing, a short list of the eight review
criteria, and the existing `pre_existing` tagging contract.

## Context

Parent mid-level: flip PR admission to surface-first. Sibling leaves own
admission/fallback dispatch and broad surface-first test rewrites. This leaf
is prompt-copy only inside the PR review path.

Today the two helpers share `_PRE_EXISTING_TAG_INSTRUCTIONS` but differ only in
mode framing ("complete files" vs "diff hunks"). `_run_reviewer` picks one or
the other per attempt. The default CODE_REVIEW profile checklist rewrite (12 →
8 criteria) is a separate prompts leaf; this note still lists the eight so PR
review steers correctly before that rewrite lands.

## Decisions

| Topic | Choice |
|---|---|
| Helper shape | One `_diff_first_focus(body)`; delete the two mode-specific helpers |
| Mode-specific framing | Dropped (input shape already implies `files=` vs `code=` / `pre_numbered`) |
| Eight criteria | Briefly enumerated in the shared note (epic checklist wording) |
| `pre_existing` copy | Reuse `_PRE_EXISTING_TAG_INSTRUCTIONS` verbatim |
| Prefix | Keep `REVIEW_FOCUS_NOTE_PREFIX` (`"Review focus:"`) |
| Blank body | Same contract: blank/whitespace `body` → note alone; else `body\n\n{note}` |
| Call sites | Every `_run_reviewer` attempt uses `_diff_first_focus(pr.body or "")` |
| Thin wrappers | Not kept |

## Note body

Order inside the appended note:

1. `REVIEW_FOCUS_NOTE_PREFIX` plus diff-first framing: evaluate what this PR
   changes (and enclosing constructs when shown); treat surrounding/unchanged
   code as context, not the primary target.
2. Short numbered list of the eight criteria:
   1. Logical / syntactic correctness of the change
   2. Contract changes on touched functions/classes (DbC, signatures, invariants)
   3. Side effects on callers of those encapsulating constructs
   4. Architectural standards
   5. Language / library / framework best practices
   6. New issues introduced by the change
   7. Does the change actually implement/fix the ticket/spec?
   8. Project style preferences
3. `_PRE_EXISTING_TAG_INSTRUCTIONS` unchanged (`pre_existing: true` /
   `pre_existing: false` contract).

Exact prose may be tightened for token cost, but must keep the prefix, the
eight items recognizably present, and both tag directions.

## Scope

### In scope

- `_diff_first_focus` + removal of `_whole_file_focus` / `_hunk_review_focus`
- `_run_reviewer` `task_requirements=` call sites
- Unit/helper tests that name or differentiate the old helpers
- Minimal comment/assertion updates in `test_coding_team_review_pr.py` that
  break solely because of the rename or the end of mode-divergent wording

### Out of scope

- Rewriting `profiles.py` CODE_REVIEW checklist
- Admission mode flip / whole-file fallback-only partition
- Scoped tools
- Broad surface-first preference test suite rewrite (sibling leaf)

## Testing

- Replace dual focus unit classes with `TestDiffFirstFocusUnit`: blank body,
  whitespace-only body, non-blank prefix, both `pre_existing` directions, and
  coverage that all eight criteria appear (or equivalent stable substrings).
- Delete the "hunk wording differs from whole-file" regression test.
- `_run_reviewer` tests: every attempt’s `task_requirements` equals
  `_diff_first_focus(...)`.
- Keep `REVIEW_FOCUS_NOTE_PREFIX` / `pre_existing` presence asserts in the
  larger PR review suite; drop hunk-vs-whole wording divergence asserts.

## Risks

- Listing eight criteria in `task_requirements` while the system prompt still
  has the 12-item checklist may briefly double-steer until the prompts leaf
  lands; accepted so this leaf stands alone.
- Tests that hard-equal old helper output must be updated in the same change
  or CI fails on the rename.
