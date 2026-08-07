# Design: Question-processing scope regression tests

Date: 2026-08-07

## Goal

Add focused regression tests that lock the documented scope decision in
`product_requirements_analysis_agent/question_processing.py`: question and
option text is logged in full, and `MAX_*` constants are intentional
item-count UX caps—not character limits on text fields.

## Context

A documentation clarification on `main` states that scope decision in the
module docstring and on the `MAX_*` comment block. Without tests, those
sentences can drift or disappear unnoticed. This change covers only the
doc/comment contract; production code is unchanged.

## Decisions

| Topic | Choice |
|---|---|
| What to assert | Module `__doc__` phrases + source comment near `MAX_ISSUES` |
| Where to put tests | Existing `test_product_requirements_analysis_agent.py` |
| Assertion style | Normalized whitespace (`" ".join(...split())`), matching SE suite docstring checks |
| Caplog / logging behavior | Out of scope |
| Production code | Unchanged |

## Scope

### In scope

- One or two tests in
  `backend/agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py`
  that:
  - Assert `question_processing.__doc__` contains wording for full-text
    logging of question/option fields and for item-count UX caps (not
    character limits).
  - Assert the module source text around `MAX_ISSUES` includes the
    item-count vs character-limit comment.

### Out of scope

- Caplog tests for truncation of logged question/option strings
- Changing `question_processing.py` or any other production module
- Unrelated test refactors or suite restructuring

## Implementation notes

- Prefer reading the module via import for `__doc__`; for the comment,
  read `Path(question_processing.__file__).read_text()` (or equivalent)
  and assert a stable substring near `MAX_ISSUES`.
- Do not cite external trackers in test names, docstrings, or comments.
- Tests must fail if the clarifying sentences are removed from the
  docstring or `MAX_*` comment.

## Acceptance

- New test(s) pass on current `main` (with the clarification present).
- Removing the clarification from the docstring or `MAX_*` comment would
  fail the new test(s).
- Existing related package tests still pass.
- Ruff for the touched test file passes.
