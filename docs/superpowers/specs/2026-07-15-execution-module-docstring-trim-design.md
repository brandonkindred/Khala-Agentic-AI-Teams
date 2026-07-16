# Trim Shared Execution Module Docstring

**Status:** Approved 2026-07-15  
**Date:** 2026-07-15  
**Type:** Documentation maintainability (behavior-preserving)  
**Tracks:** GitHub issue maintainability flag on `shared/phases/execution.py` module docstring

## Problem

`software_engineering_team/shared/phases/execution.py` opens with a ~23-line module docstring that enumerates internal design decisions: which helpers live here, backend vs frontend gate call shapes, `GatedExecutionConfig` field kinds, and the gated-loop skeleton phases. That narrative belongs near the symbols it describes (or is already there). A module docstring should summarize purpose, not restate the architecture.

## Goals

1. Replace the module docstring with a concise purpose summary.
2. Relocate the two unique facts that are not already local to nearby docs/comments.
3. Leave runtime behavior unchanged.

## Non-goals

- Rewriting `GateOutcome` / `GatedExecutionConfig` / `run_gated_execution_impl` docs beyond the small unique-fact additions.
- Adding or updating a separate design document for the gated loop itself.
- Refactoring call sites, types, or tests (except if a docstring-only assertion exists — none expected).

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Surgical relocate + trim | Most module prose is already on classes/fields/`run_gated_execution_impl` |
| Unique fact 1 | Gate-adapter field comment | Backend vs frontend review call shapes are not named on the field comment today |
| Unique fact 2 | `run_general_microtask` docstring | `EXECUTION_PROMPT` / `{language_conventions}` via `StackProfile` is only sketched in preconditions |
| Extra design doc | None beyond this spec | Issue is docstring hygiene; gated-loop design already lives in prior specs |

## Changes

### Module docstring

Replace with:

```text
Shared Execution-phase leaf helpers for the code-v2 teams, including the
gated per-microtask review loop (``run_gated_execution_impl``).
```

### Gate-adapter field comment (`GatedExecutionConfig`)

Extend the existing comment above `run_code_review_gate` / sibling gates to state:

- Backend adapters wrap separate `run_{code_review,qa,security}_testing_phase` calls returning `PhaseReviewResult`.
- Frontend adapters call unified `run_microtask_review()` three times and filter issues by `source`.
- All adapters normalize into `GateOutcome`.

### `run_general_microtask` docstring

Add a short note that stack-specific `EXECUTION_PROMPT` divergence is owned by `StackProfile`: backend templates include a `{language_conventions}` slot; frontend templates do not (same precondition already asserts the slot/profile pairing).

## Success criteria

- Module docstring is ≤ ~4 lines and states purpose + the gated-loop entrypoint.
- Backend/frontend gate call-shape and `EXECUTION_PROMPT`/`StackProfile` notes appear only near their symbols (plus any pre-existing coverage such as `GateOutcome`).
- No code, import, or test behavior changes; targeted SE gating tests still pass if run.

## Risk

Negligible. Documentation-only edits; failure mode is losing a unique rationale — mitigated by the two explicit relocations above.
