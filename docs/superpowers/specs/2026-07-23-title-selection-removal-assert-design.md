# Design: Assert disliked title removed on LLM failure

**Issue:** #2360  
**Branch / worktree:** `fix/2360-title-selection-removal-assert`  
**Date:** 2026-07-23

## Problem

`test_run_title_selection_llm_failure_falls_back_to_removal` in
`backend/agents/blogging/tests/test_v2_helpers_extra.py` claims (name +
docstring) that when the LLM fails while generating a replacement, the
disliked title is removed from the candidate list. The body only asserts
the HITL fallback return value (`"Fallback Title"`) and never verifies
removal. That mismatch misleads maintainers about what the test covers.

A suggested assert on `plan.title_candidates` would be wrong: production
`_run_title_selection` copies candidates into a local `title_choices`
list and, on LLM failure, filters that list, then passes it to
`job_updater` as `title_choices=...`. The `ContentPlan` is not mutated.

## Goal

Keep the existing test name and docstring. Strengthen the test so it
records `title_choices` from `job_updater` and asserts the disliked title
(`"First"`) is absent after the LLM-failure path.

## Non-goals

- No production code changes in `pipeline/_common.py` or the v2 shim.
- No rename of the test or rewrite of its docstring.
- No assert on `plan.title_candidates` (wrong object).
- No changes to sibling title-selection tests in `test_v2_title_selection.py`.

## Design

### File touched

Only `backend/agents/blogging/tests/test_v2_helpers_extra.py`, function
`test_run_title_selection_llm_failure_falls_back_to_removal`.

### Recording updater

Replace the no-op `job_updater=lambda **kw: None` with a recorder:

```python
plan = _plan()
recorded_choices: list = []

def updater(**kw):
    choices = kw.get("title_choices")
    if choices is not None:
        recorded_choices.append(choices)

out = _run_title_selection(
    plan=plan,
    llm_client=_AngryLLM(),
    job_id=job_id,
    job_updater=updater,
    _update=lambda phase, **kw: None,
)
assert out == "Fallback Title"
assert recorded_choices, "expected job_updater to receive title_choices after LLM failure"
assert all(
    candidate.get("title") != "First"
    for snapshot in recorded_choices
    for candidate in snapshot
)
```

Existing setup (pending dislike for `"First"`, `_AngryLLM`, `fake_sleep`
selecting `"Fallback Title"`) stays as-is.

## Testing

From `backend/`:

```bash
pytest agents/blogging/tests/test_v2_helpers_extra.py::test_run_title_selection_llm_failure_falls_back_to_removal -q
```

Optionally the full file:

```bash
pytest agents/blogging/tests/test_v2_helpers_extra.py -q
```

## Success criteria

1. Test still named `test_run_title_selection_llm_failure_falls_back_to_removal`
   with the existing removal-focused docstring.
2. Asserts both fallback title and absence of `"First"` in recorded
   `title_choices` snapshots.
3. Does not assert on `plan.title_candidates`.
4. Production code unchanged.
5. Focused pytest passes.
