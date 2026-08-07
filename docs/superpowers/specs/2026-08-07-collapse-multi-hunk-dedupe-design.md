# Design: Collapse — multi-hunk same-construct dedupe (AC regression)

Date: 2026-08-07

## Goal

Prove that when a single-file patch has multiple hunks touching the same
enclosing construct, the change surface emits that construct span once.

## Context

Production assembly already collapses this case. `_assemble_path_block`
collects all added touched lines across hunks, expands each to an enclosing
construct via `expand_touched_ranges` (which dedupes identical ranges), then
`_merge_line_ranges` before `_pre_number_ranges`. Expand-level coverage exists
for multiple touched lines in one function; what is missing is an assembly-level
test whose input is a real two-`@@` patch.

Verified on current `main`: a two-hunk edit inside one Python function yields a
single pre-numbered body with one `def` header and no `...` gap marker.

## Decisions

| Topic | Choice |
|---|---|
| Production code | No change |
| Mechanism | Existing expand dedupe + `_merge_line_ranges` (Approach A: test only) |
| Test home | `test_change_surface_from_patches.py` |
| Assertion focus | One construct span in the assembled body (not expand-only) |

## Scope

### In scope

- One unit test: two hunks in one function → single span in
  `build_change_surface_from_patches` output
- Optional exact body equality to the expected pre-numbered construct

### Out of scope

- New collapse helper or API
- No-AST context capping / separators hardening (sibling leaf)
- Admission wiring or scoped tools
- Changing expand / merge / assemble behavior

## Test contract

Input:

- New-file content: a multi-line Python function (enough lines for two
  non-adjacent added lines inside the same construct).
- Patch: two unified-diff hunks, each with at least one `+` line inside that
  function.

Expected:

- Surface is non-empty for that path.
- Body contains the function definition line exactly once.
- Body has no `...` gap marker (ranges merged into one contiguous span).
- Prefer asserting the full pre-numbered body string for the construct.

## Non-goals / YAGNI

- Re-testing expand dedupe in isolation (already covered).
- Multi-file or cross-construct cases in this leaf.
- Documenting collapse as a separate public API.
