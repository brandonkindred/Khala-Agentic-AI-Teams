# Design: Question-processing character-limit scope clarification

Date: 2026-08-07

## Goal

Make the character-limit vs item-count scope decision explicit in
`product_requirements_analysis_agent/question_processing.py` so future reviews
do not treat intentional `MAX_*` item caps as missing text-field truncations,
and so the already-landed “log full question/option text” behavior stays
documented at the module boundary.

## Context

A prior truncation-removal refactor removed log-message character slices on
question and option text in this file (`question_text[:60]`, `opt_label[:50]`,
`existing[:50]`). Those removals are already on `main`. An automated
spec-compliance review still flagged the file as a scope-alignment gap because
it still contains limit-related symbols — but the remaining `MAX_ISSUES`,
`MAX_GAPS`, and `MAX_OPEN_QUESTIONS` constants are **item-count** UX caps (keep
a single spec-review sitting digestible), not character limits on text fields
fed to the LLM or logged to operators.

The production behavior that applies here is already correct. What is missing
is an explicit, durable statement of that scope decision in the module itself.

## Decisions

| Topic | Choice |
|---|---|
| Production behavior change | None — keep full-text logging; keep `MAX_*` item caps |
| Documentation location | Module docstring + one-line note on the existing `MAX_*` comment block |
| Malformed-response log preview (`preview[:200]`) | Leave as-is (out of scope; not a text-field / prompt truncation) |
| Regression tests | Separate follow-on (not this change) |

## Scope

### In scope

- Update the module docstring of
  `backend/agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py`
  to state that:
  - question and option text is logged in full (no character truncation for
    those fields);
  - `MAX_ISSUES` / `MAX_GAPS` / `MAX_OPEN_QUESTIONS` are intentional item-count
    UX caps, not character limits on text fields.
- Add a one-line clarification on the existing comment above the `MAX_*`
  constants to the same effect.

### Out of scope

- Removing or raising `MAX_*` caps
- Changing `preview[:200]` for malformed LLM JSON logging
- Changing stem/morphology string slices or `depends_on` list shortening
- Adding or updating tests (follow-on work)
- Edits outside `question_processing.py`

## Implementation notes

- Keep wording short and contract-oriented; do not cite external trackers in
  the docstring or comments.
- Do not alter runtime logic, log format strings, or constant values.
- Lint only the touched file after the edit.

## Acceptance

- Module docstring and `MAX_*` comment make the item-count vs character-limit
  distinction explicit.
- No runtime behavior change.
- Ruff lint for the touched file passes.
