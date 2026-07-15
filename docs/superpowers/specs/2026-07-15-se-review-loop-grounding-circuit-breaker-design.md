# SE Review Loop: Grounding-Failure Circuit Breaker

**Status:** Approved 2026-07-15  
**Date:** 2026-07-15  
**Type:** Circuit breaker for hallucination-driven CR↔fix loops  
**Issue:** GitHub #1276 (PR body only; do not cite in code)  
**Depends on:** #1277 (`record_gate_outcome` wiring — landed); LLM issue grounding (#1274 — landed)

## Problem

`run_gated_execution_impl` can spend its full outer-cycle budget (and inner CR batch-fix retries) chasing findings that were fabricated and only partially caught by grounding. A new fake claim each cycle evades exact-string dedup; `_dedup_issues` exists but is unused. Nothing stops the loop early based on a high grounding *drop* ratio.

## Goals

1. Fail the microtask early when consecutive **outer** cycles show a high grounding-rejection ratio while code review is still failing.
2. Ship **conservative** defaults (`cycle_limit=3`, `ratio=0.75`); `cycle_limit≤0` disables the breaker.
3. Plumb accurate pre-grounding `raw_issue_count` into `GateOutcome` via the LLM-fallback CR path.
4. Wire `_dedup_issues` before batch fixes as a cheap exact-repeat suppressor.
5. Record trips via `_record_terminal_gate_failure("review_grounding_circuit_breaker", ...)`.

## Non-goals

- Changing grounding phrase / corpus rules.
- Grounding or ratio tracking for QA/security gates.
- Recording write-path failures under the circuit-breaker gate string.
- Tuning thresholds from production telemetry in this change (defaults are intentionally conservative).

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Plumb count on existing review result surfaces | Accurate ratio; matches issue intent |
| Defaults | `cycle_limit=3`, `ratio=0.75` | Observability is live; avoid aggressive false trips before data accumulates |
| Kill switch | `grounding_failure_cycle_limit ≤ 0` | No extra boolean required |
| Evaluate when | Once when leaving the CR section of an outer cycle | One streak tick per outer cycle; lower FP than per-attempt ticks |
| Bad cycle | Any CR call in that outer cycle had `not passed` **and** ratio ≥ threshold | Needed for QA/sec restart loops (CR may pass after a bad failing call); pass-only cycles never count |
| Missing raw | `None` / `≤0` → not-bad (reset streak) | In-process agent / QA-sec / grounding-off stay safe |
| Trip recording | `review_grounding_circuit_breaker` via existing helper | Consistent with #1277 telemetry |

## Architecture

```
run_llm_review
  → parse issues (raw_n)
  → drop_ungrounded_issues → kept
  → return kept + raw_n
       ↓
PhaseReviewResult / ReviewResult.raw_issue_count
       ↓
CR gate adapter → GateOutcome.raw_issue_count
       ↓
run_gated_execution_impl
  → on each CR gate call: if not passed and ratio ≥ threshold → cycle_bad
  → when leaving CR section: streak++ if cycle_bad else reset
  → if streak ≥ limit: REVIEW_FAILED + record gate (before QA if CR passed)
```

Also: per-microtask `seen` set; `_dedup_issues(issues, seen)` before every `run_batch_coding_fixes` (CR/QA/security).

## Count plumbing

- Add `raw_issue_count: Optional[int] = None` to:
  - `GateOutcome`
  - shared `ReviewResult`
  - BE `PhaseReviewResult`
- `run_llm_review`: capture `raw_n = len(issues)` **before** `drop_ungrounded_issues`; surface `raw_n` with the kept list (small result type or equivalent). Callers that ignore the count keep behavioral parity for the issue list.
- BE/FE `_run_llm_review` / `run_code_review_phase` / unified FE review: forward the count onto the review result.
- CR gate adapters: `GateOutcome(..., raw_issue_count=getattr(r, "raw_issue_count", None))`.
- In-process `code_review_agent` path, QA, security: leave `None`.

## Loop semantics

**Config** on `BaseMicrotaskReviewConfig`:

- `grounding_failure_cycle_limit: int = 3` (`≤0` disables)
- `grounding_failure_ratio_threshold: float = 0.75` (clamp to `[0.0, 1.0]` when reading)

**Ratio** for a CR `GateOutcome` call:

```text
ratio = (raw_issue_count - len(issues)) / raw_issue_count
```

only when `raw_issue_count` is an `int > 0`; otherwise that call is not-bad.

**Why not “settled outcome not passed” alone:** a failing settled CR always ends the
microtask via retry exhaustion, so multi-outer-cycle streaks cannot accumulate
that way. Production `max_total_cycles` burns use CR-pass-then-QA/sec-restart.

**Streak** (per microtask, across outer cycles):

- During the CR section, set `cycle_bad` if **any** CR gate call (initial or
  inner retry) has `not passed` and ratio ≥ threshold.
- When leaving the CR section (pass → QA, or fail → terminal): if `cycle_bad`
  then `streak += 1`, else reset to `0`. Pass-only cycles (no failing high-ratio
  CR call) never increment — including CR pass after drops with no prior bad call.
- Missing raw on a call does not mark that call bad.

**Trip** (prefer a helper to avoid C901 growth):

- When `streak >= limit` on leaving the CR section: set `REVIEW_FAILED`,
  rollback microtask files, distinct diagnostic `mt.notes`.
- `_record_terminal_gate_failure("review_grounding_circuit_breaker", cr_outcome, task_id)`.
- Prefer breaker over ordinary retry-exhaustion when both would apply; if CR
  passed this cycle but streak hit the limit, trip before QA.
- Honor `on_failure` / stop-raise the same way as other terminal CR failures.

## `_dedup_issues`

- One `seen: set[tuple[str, str]]` per microtask for the gated loop lifetime.
- Before each `run_batch_coding_fixes` call (CR inner retries, QA, security), replace `issues` with `_dedup_issues(issues, seen)`.
- Exact repeats across cycles never re-enter the fixer; novel wording still does (breaker covers that).

## Data flow & failure modes

| Case | Outcome |
|---|---|
| 3 consecutive bad outer CRs (defaults) | Early `REVIEW_FAILED` + circuit-breaker telemetry |
| Low drop ratio, CR still failing | Full outer-cycle budget |
| CR passes after drops | Streak reset; no trip |
| `raw_issue_count` missing | Not-bad; streak reset |
| `cycle_limit ≤ 0` | Breaker off |
| Exact duplicate issue next cycle | Deduped before batch fix |

## Testing

1. **`test_shared_llm_review.py`** — `raw_issue_count` equals pre-grounding size; kept list smaller when drops occur.
2. **Pure ratio/streak helper tests** — bad / not-bad / missing raw / disabled limit.
3. **`test_v2_gated_execution_shared.py`**:
   - Hallucination-loop: distinct high-ratio failing CRs → trip within `cycle_limit`; one `review_grounding_circuit_breaker` record.
   - Low ratio still failing → no trip.
   - CR pass after heavy drops → streak reset.
   - Exact-repeat across cycles → not passed to `run_batch_coding_fixes` the second time.
   - `cycle_limit=0` → disabled.

## Files

| Path | Change |
|---|---|
| `shared/llm_review.py` (+ BE/FE review callers) | Surface pre-grounding count |
| `shared/v2_models.py` | Config knobs; `ReviewResult.raw_issue_count` |
| `backend_code_v2_team/models.py` | `PhaseReviewResult.raw_issue_count` |
| BE/FE CR gate adapters | Copy count into `GateOutcome` |
| `shared/phases/execution.py` | Streak, trip helper, dedup wiring |
| Tests listed above | Coverage |

## Implementation notes

- DbC on new helpers (`Preconditions` / `Postconditions`).
- TDD: failing tests for plumbing + streak + gated-loop regressions first.
- Do not mention GitHub issue numbers in code/comments/commits; use `Closes #1276` only in the PR body.
- Keep C901 under control — extract trip/ratio update into helpers (same pattern as `_apply_code_review_retry_exhausted`).
