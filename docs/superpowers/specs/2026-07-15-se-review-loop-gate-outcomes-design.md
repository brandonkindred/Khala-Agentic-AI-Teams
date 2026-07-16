# SE Review Loop: Wire `record_gate_outcome` Observability

**Status:** Approved 2026-07-15  
**Date:** 2026-07-15  
**Type:** Observability / closed-loop learning for gated review failures  
**Issue:** GitHub #1277 (PR body only; do not cite in code)  
**Blocks:** #1276 (grounding-failure circuit breaker — needs rejection telemetry before threshold tuning)

## Problem

`shared/gate_outcomes.py::record_gate_outcome` already writes a `gate_rejected` DORA event and upserts a `se_learnings` row on gate rejection — but it is never called from `run_gated_execution_impl` or anywhere else in the gated loop. Review-gate failures, including hallucination-driven ones, are invisible operationally and do not feed forward into future Tech Lead Design prompts.

A second gap blocks wiring today: `is_rejected` only understands `approved` and `all_satisfied`, while the shared loop normalises gate results into `GateOutcome` (`passed` / `issues` / `summary`). Passing a `GateOutcome` through the hook today would silently no-op.

## Goals

1. Record terminal `REVIEW_FAILED` outcomes from the shared gated loop as DORA events + learnings.
2. Teach `is_rejected` to understand `passed=False` so `GateOutcome` works without adapters.
3. Provide a single helper seam in `execution.py` for the companion circuit-breaker issue (#1276) to reuse.
4. Keep loop behaviour unchanged — observability only, best-effort, never-raising.

## Non-goals

- Wiring write-path failures (`write_microtask_output_or_fail` / unsafe repo path) — not quality-gate rejections.
- Mid-loop QA/security batch-fix `continue`s — microtask is not terminal yet.
- Threading `job_id` through the call chain (pass `job_id=""` per existing contract).
- Implementing the grounding circuit breaker (#1276) in this change.
- Changing review quality, grounding filters, or retry budgets.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Thin `_record_terminal_gate_failure` helper in `execution.py` | One seam for current + future terminal failure sites |
| `is_rejected` bridge | Add `passed` (bool) after `approved` / `all_satisfied` | Smallest change; `GateOutcome` works directly |
| Terminal sites wired | Code-review retry exhaustion + max-cycles only | Matches issue scope; write failures excluded |
| `job_id` | Always `""` at call sites | Avoids signature threading in this PR |
| Gate strings | `code_review_retry_exhausted`, `review_max_cycles` | Distinct `se_learnings.category` per failure mode |
| Future breaker string | `review_grounding_circuit_breaker` (reserved) | #1276 adds one call via same helper |

## Architecture

```
run_gated_execution_impl
  └─ on terminal REVIEW_FAILED:
       _record_terminal_gate_failure(gate, outcome, task_id)
         └─ record_gate_outcome(gate, outcome, job_id="", task_id=..., phase="execution")
              └─ is_rejected(outcome)   # now: approved | all_satisfied | passed
              └─ se_events.record_event + upsert_learning
```

### `is_rejected` precedence

1. `approved` (bool) — existing
2. `all_satisfied` (bool) — existing
3. `passed` (bool) — **new**: `passed=False` → rejected
4. `None` if none present

### `_record_terminal_gate_failure(gate, outcome, task_id)`

- Preconditions: `gate` is a non-empty string; `outcome` is a duck-typed gate result.
- Postconditions: delegates to `record_gate_outcome`; never raises; always passes `job_id=""`, `phase="execution"`.

### `_terminal_failing_outcome(cr, qa, sec) -> GateOutcome`

- Returns the first still-failing outcome in order: code review → QA → security.
- When all three passed but `max_cycles_requires_failing_gate=False` still marks `REVIEW_FAILED`, returns `GateOutcome(passed=False, summary="Max cycles exceeded")`.

## Call sites

**Site 1 — Code-review retry exhaustion** (`execution.py`, ~line 728)

- Gate: `code_review_retry_exhausted`
- Result: live `cr_outcome` (issues + summary already populated)
- Timing: once, immediately after `REVIEW_FAILED` is set, before rollback / `on_failure` raise

**Site 2 — Max-cycles exceeded** (`execution.py`, ~line 918)

- Gate: `review_max_cycles`
- Result: `_terminal_failing_outcome(cr_outcome, qa_outcome, sec_outcome)`
- Timing: same as site 1

**Not wired**

- `write_microtask_output_or_fail` failures
- QA/security mid-loop batch-fix restarts
- Successful microtasks

## Data flow & failure modes

| Case | Outcome |
|---|---|
| Terminal `REVIEW_FAILED` | One `record_gate_outcome` call with distinct `gate` |
| Gate passes | No call |
| Postgres disabled | `record_gate_outcome` returns `False`; loop unaffected |
| Exception inside hook | Caught inside `record_gate_outcome`; loop unaffected |

## Testing

1. **`tests/test_learnings_ingest.py`**
   - `is_rejected(GateOutcome(passed=False))` → `True`
   - `is_rejected(GateOutcome(passed=True))` → `False`
   - Existing `approved` / `all_satisfied` regressions unchanged

2. **`tests/test_v2_gated_execution_shared.py`** (monkeypatch `record_gate_outcome`)
   - Code review fails through retry cap → exactly one call, `gate="code_review_retry_exhausted"`, `task_id` set, `phase="execution"`
   - Loop hits `max_total_cycles` still failing → exactly one call, `gate="review_max_cycles"`
   - All gates pass → zero calls
   - QA fails once, batch-fix, then passes → zero calls

## Files

| Path | Change |
|---|---|
| `shared/gate_outcomes.py` | `is_rejected` understands `passed` |
| `shared/phases/execution.py` | `_record_terminal_gate_failure`, `_terminal_failing_outcome`, two call sites |
| `tests/test_learnings_ingest.py` | `passed` coverage |
| `tests/test_v2_gated_execution_shared.py` | Wiring regression tests |

## Implementation notes

- Follow DbC on every new public/private helper (`Preconditions` / `Postconditions` in docstrings).
- TDD: failing tests for `is_rejected` + gated-loop wiring first, then implement.
- Do not mention GitHub issue numbers in code, comments, or commit messages; use `Closes #1277` only in the PR body.
- #1276 follows in a separate PR, adding `review_grounding_circuit_breaker` via the same helper.
