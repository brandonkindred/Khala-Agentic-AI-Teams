# Design: Wire real Phase 4 evidence into acceptance-criterion `tests`

**Issue:** #2260  
**Branch / worktree:** `fix/2260-acceptance-criterion-tests-trace`  
**Date:** 2026-07-24

## Problem

In `backend/agents/software_engineering_team/devops_team/orchestrator.py`, Phase 5
builds every `CriterionTrace` with a hard-coded

```python
tests=[{"validation": "pass"}]
```

regardless of whether Phase 4 validation produced any test evidence. That makes
`acceptance_criteria_trace` inaccurate and can mislead downstream compliance
checks.

Phase 4 already runs `test_validation_agent` and receives
`val.acceptance_trace` — a list of `{criterion, implementation_refs, tests}`
dicts — but that value is scoped only inside `_phase4_validation_review` and is
never used when assembling the completion package.

## Goal

Populate each criterion’s `tests` (and, when Phase 4 provides a match,
`implementation_refs`) from Phase 4’s `acceptance_trace`. When no matching
evidence exists, use an honest empty `tests=[]` rather than inventing a pass.

## Non-goals

- No changes to `CriterionTrace` / completion-package schemas.
- No changes to QA acceptance-evidence prompts or the validation agent’s
  mapping logic.
- No ownership move of the trace into the doc/runbook agent.
- No synthesis of per-criterion `tests` from `quality_gates` /
  `validation_evidence` when the trace is empty.

## Design

### Approach

Promote Phase 4’s `acceptance_trace` to `_run_pipeline` scope (same pattern as
`quality_gates`), then build `completion.acceptance_criteria_trace` from it in
Phase 5.

### Pipeline scope

In `_run_pipeline`, introduce a list shared with Phase 4+:

```python
acceptance_trace: List[Dict[str, object]] = []
```

Inside `_phase4_validation_review`, after `val = self.test_validation_agent.run(...)`:

```python
nonlocal aggregated_artifacts, quality_gates, acceptance_trace
...
acceptance_trace = list(val.acceptance_trace)
```

### Phase 5 mapping

Replace the hard-coded list comprehension with a per-criterion map over
`task_spec.acceptance_criteria`:

1. Find the first Phase 4 entry whose `criterion` equals `c` (string compare on
   `str(entry.get("criterion", ""))`).
2. **Match found:** build `CriterionTrace` from that entry:
   - `criterion=c`
   - `implementation_refs` / `tests` coerced as below
3. **No match:**  
   `CriterionTrace(criterion=c, implementation_refs=sorted(aggregated_artifacts.keys()), tests=[])`

### Coercion rules

Phase 4 entries are `Dict[str, object]`, not typed `CriterionTrace`:

- Non-list `implementation_refs` or `tests` → `[]`
- `tests` list items that are not `dict` → drop
- Remaining `tests` dict values → `str(...)` so the field stays
  `List[Dict[str, str]]`
- Never invent a `"pass"` (or any other) status

### Helper placement

A small private helper on the orchestrator (or a module-level function next to
it) keeps Phase 5 readable, e.g.
`_criterion_traces_from_phase4(criteria, acceptance_trace, artifact_keys) -> List[CriterionTrace]`.
DbC docstring required (preconditions / postconditions).

## Testing

Files: `backend/agents/software_engineering_team/tests/test_devops_team.py`
(and the orchestrator under test).

1. Update `_scripted_llm_for_happy_path` (and any sibling scripts that stub
   validation) so the validation response includes a real `acceptance_trace`
   for at least one criterion with non-empty `tests`.
2. Extend `test_completion_package_has_acceptance_trace` (or add a sibling):
   - Matched criteria keep Phase 4 `tests` and `implementation_refs`.
   - Unmatched criteria get `tests == []` and artifact-key refs.
3. Assert completion traces do not contain `{"validation": "pass"}` unless
   Phase 4 actually returned that dict.

Focused run from `backend/`:

```bash
pytest agents/software_engineering_team/tests/test_devops_team.py -k "acceptance_trace or completion_package_has_acceptance" -q
```

## Success criteria

1. Orchestrator no longer hard-codes `tests=[{"validation": "pass"}]`.
2. Phase 4 `acceptance_trace` is visible to Phase 5 and drives matching traces.
3. Missing evidence yields `tests=[]`, not a fabricated pass.
4. Focused devops-team tests covering the mapping pass.
