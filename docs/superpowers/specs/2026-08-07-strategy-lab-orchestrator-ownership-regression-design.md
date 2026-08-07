# Design: Strategy Lab orchestrator ownership regression coverage

Date: 2026-08-07

## Goal

Lock the post-extract ownership of Strategy Lab orchestration helpers so
`api/main.py` cannot silently regain `def` bodies for symbols that now live in
`strategy_lab/orchestrator_api.py`, while proving routes still resolve the same
callables via hybrid aliases.

This is the verification slice after the inventory, persist/reconcile/purge, and
dispatch/guards extracts. It does **not** move finalize or other still-deferred
Temporal-hot helpers.

## Context

`test_orchestrator_api.py` already asserts:

- Moved symbols are real attributes on `orchestrator_api.__dict__`
- Each moved symbol is identical (`is`) to the `api.main` alias
- Deferred symbols still resolve via `__getattr__` to `api.main`

Missing: a regression that fails if a moved helper’s **function body** is pasted
back into `api/main.py` (aliases would still satisfy the `is` check if someone
redefined locally and rewired poorly, or more commonly if a future edit
duplicates a `def` while also keeping an alias — AST catches the `def`).

Acceptance criteria naming “finalize” is interpreted under the deliberate
narrow scope: finalize remains deferred; coverage for it is façade identity
only until a follow-up body move. Dispatch / persist / purge (and cluster-2
guards) are covered by ownership + existing route/Temporal suites.

## Decisions

| Topic | Choice |
|---|---|
| Scope | Ownership + identity only; no new behavioral smokes |
| Finalize | Deferred; not in ownership guard |
| Guard mechanism | AST parse of `api/main.py`; forbid `FunctionDef`/`AsyncFunctionDef` for moved callables |
| Location | Extend `test_orchestrator_api.py` |
| Constants | Not checked as function defs (they are assignments); identity `is` still covers them |
| Docs | Test module docstring + short note in `ORCHESTRATOR_API_BOUNDARIES.md` |

## Scope

### In scope

1. Define `_MOVED_CALLABLES` as the callable subset of `_MOVED` (exclude
   `STRATEGY_LAB_TERMINAL_STATUSES`).
2. Add `test_api_main_has_no_moved_helper_function_bodies` that:
   - Loads `api.main` source
   - Parses with `ast`
   - Collects top-level function def names
   - Asserts none of `_MOVED_CALLABLES` appear
3. Keep existing identity / deferred / `__dir__` / constant export tests.
4. One short boundaries-doc sentence under the partial-move section pointing at
   the ownership regression test.
5. Smoke: Strategy Lab route-related tests still pass (no new route tests).

### Out of scope

- Extracting `_finalize_strategy_lab_cycle_record` or other `_DEFERRED` bodies
- New behavioral unit tests that re-exercise persist/dispatch/purge logic
- Shell/CI `rg` gate outside pytest
- Non–Strategy-Lab restructuring of `api/main.py`

## Architecture

```
test_orchestrator_api.py
  _MOVED / _DEFERRED          (existing identity parametrization)
  _MOVED_CALLABLES            (callables only)
  test_*_defined_on_orchestrator_api
  test_deferred_symbol_still_aliases_api_main
  test_api_main_has_no_moved_helper_function_bodies   ← NEW
       → ast.parse(api.main source)
       → forbid FunctionDef names ∩ _MOVED_CALLABLES
```

Existing Strategy Lab route + Temporal suites remain the behavioral gate for
dispatch/persist/purge through aliases.

## Error handling / false positives

- Assignment aliases (`_persist_run_state = orchestrator_api._persist_run_state`)
  are **not** function defs — allowed.
- Nested functions inside unrelated helpers are ignored if the walk is
  top-level-only (helpers were never nested defs in `api.main`). Prefer
  top-level walk to avoid flagging inner helpers with colliding names.
- Deferred names may still have real `def` bodies in `api.main` — not in
  `_MOVED_CALLABLES`.

## Testing

- Unit: new ownership test green on current tree
- Negative sanity (manual or temporary): adding a stub
  `def _persist_run_state(...): ...` in `api.main` must fail the test (do not
  commit the stub; verify during implementation then revert)
- Smoke: `test_orchestrator_api.py` + `test_strategy_lab_routes.py` (or the
  Investment Strategy Lab CI subset) pass

## Success criteria

- [ ] AST ownership guard present and green
- [ ] Moved callables cannot reappear as top-level function defs in `api/main.py`
- [ ] Identity + deferred tests still green
- [ ] Boundaries note mentions the regression guard
- [ ] Strategy Lab route smoke passes

## Risks

| Risk | Mitigation |
|---|---|
| Walking all nested defs false-positives | Top-level module body only |
| `_MOVED` drifts from reality | Same list drives identity + guard; update list when next extract lands |
| Finalize AC wording mismatch | Spec documents deferred interpretation; follow-up extract extends `_MOVED` |
