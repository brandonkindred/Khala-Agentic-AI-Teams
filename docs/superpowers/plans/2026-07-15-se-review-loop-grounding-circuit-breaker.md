# SE Review Loop Grounding Circuit Breaker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trip early `REVIEW_FAILED` when consecutive outer review cycles show high grounding-drop ratios on failing CR calls, and wire `_dedup_issues` before batch fixes.

**Architecture:** `run_llm_review` returns `LlmReviewOutput(issues, raw_issue_count)`; CR results/`GateOutcome` carry the count; the gated loop marks an outer cycle bad if any CR invocation in that cycle was `not passed` with ratio ≥ threshold, updates streak once when leaving the CR section, and trips via `_record_terminal_gate_failure("review_grounding_circuit_breaker", ...)`.

**Tech Stack:** Python 3.10+, pytest, existing gated-execution / llm_review tests.

## Global Constraints

- DbC on every new public helper.
- No GitHub issue numbers in code/comments/commits (PR body: `Closes #1276`).
- Defaults: `grounding_failure_cycle_limit=3`, `grounding_failure_ratio_threshold=0.75`; `cycle_limit ≤ 0` disables.
- Missing/`None`/`≤0` raw count → that CR call is not-bad.
- Extract helpers to avoid C901 on `run_gated_execution_impl`.

## Control-flow clarification (amend design §2)

A settled `not cr_outcome.passed` always ends the microtask via retry exhaustion — so a streak of 3 cannot accumulate on “final failing CR” alone. Production `max_total_cycles` burns happen when CR eventually passes after fixes and QA/security `continue`s the outer loop.

**Implement:**
1. During the CR section, `cycle_bad = False`. On **every** CR gate result (initial + each inner retry), if `not passed` and ratio ≥ threshold → `cycle_bad = True`.
2. When leaving the CR section (pass → QA **or** fail → terminal): if `cycle_limit ≤ 0`, skip; elif `cycle_bad`: `streak += 1`; else `streak = 0`.
3. If `streak >= limit`: circuit-breaker trip (distinct notes + `review_grounding_circuit_breaker`), even if CR later passed in that cycle (trip **before** QA). If CR still failed and streak below limit: existing retry-exhaustion path.

This keeps **one streak tick per outer cycle** while counting cycles that contained a failing high-ratio CR (including before a batch-fix pass that enabled QA restart).

Update the design doc’s “Bad cycle” wording to match in the Task 4 commit.

## File map

| Path | Responsibility |
|---|---|
| `shared/llm_review.py` | `LlmReviewOutput`; pre-grounding count |
| `shared/v2_models.py` | Config knobs; `ReviewResult.raw_issue_count` |
| `backend_code_v2_team/models.py` | `PhaseReviewResult.raw_issue_count` |
| BE/FE `phases/review.py`, `shared/v2_review.py` | Forward count from LLM path |
| BE/FE CR gate adapters | Copy into `GateOutcome` |
| `shared/phases/execution.py` | Ratio helpers, streak, trip, dedup |
| Tests | Plumbing + breaker + dedup regressions |

---

### Task 1: Config + pure ratio helpers (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/v2_models.py`
- Modify: `backend/agents/software_engineering_team/shared/phases/execution.py`
- Create: `backend/agents/software_engineering_team/tests/test_grounding_circuit_breaker.py`

**Interfaces:**
- Produces:
  - `grounding_failure_cycle_limit: int = 3`
  - `grounding_failure_ratio_threshold: float = 0.75`
  - `GateOutcome.raw_issue_count: Optional[int] = None`
  - `grounding_rejection_ratio(raw, kept) -> Optional[float]`
  - `cr_call_is_grounding_bad(passed, raw, kept, threshold) -> bool`

- [ ] **Step 1: Write failing tests**

```python
from software_engineering_team.shared.phases.execution import (
    cr_call_is_grounding_bad,
    grounding_rejection_ratio,
)
from software_engineering_team.shared.v2_models import BaseMicrotaskReviewConfig


def test_config_defaults_are_conservative():
    c = BaseMicrotaskReviewConfig()
    assert c.grounding_failure_cycle_limit == 3
    assert c.grounding_failure_ratio_threshold == 0.75


def test_grounding_rejection_ratio():
    assert grounding_rejection_ratio(4, 1) == 0.75
    assert grounding_rejection_ratio(None, 0) is None
    assert grounding_rejection_ratio(0, 0) is None


def test_cr_call_is_grounding_bad():
    assert cr_call_is_grounding_bad(
        passed=False, raw_issue_count=4, kept_count=1, ratio_threshold=0.75
    )
    assert not cr_call_is_grounding_bad(
        passed=True, raw_issue_count=4, kept_count=0, ratio_threshold=0.75
    )
    assert not cr_call_is_grounding_bad(
        passed=False, raw_issue_count=None, kept_count=1, ratio_threshold=0.75
    )
```

- [ ] **Step 2: Run FAIL**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/software_engineering_team/tests/test_grounding_circuit_breaker.py -v
```

- [ ] **Step 3: Implement config fields, `GateOutcome.raw_issue_count`, helpers**

```python
def grounding_rejection_ratio(
    raw_issue_count: Optional[int], kept_count: int
) -> Optional[float]:
    if raw_issue_count is None or raw_issue_count <= 0:
        return None
    kept = max(0, min(kept_count, raw_issue_count))
    return (raw_issue_count - kept) / float(raw_issue_count)


def cr_call_is_grounding_bad(
    *,
    passed: bool,
    raw_issue_count: Optional[int],
    kept_count: int,
    ratio_threshold: float,
) -> bool:
    if passed:
        return False
    ratio = grounding_rejection_ratio(raw_issue_count, kept_count)
    if ratio is None:
        return False
    threshold = max(0.0, min(1.0, float(ratio_threshold)))
    return ratio >= threshold
```

- [ ] **Step 4: Run PASS** then commit

```bash
git commit -m "feat: add grounding circuit-breaker config and ratio helpers"
```

---

### Task 2: `LlmReviewOutput` + plumb raw count (TDD)

**Files:**
- Modify: `shared/llm_review.py`
- Modify: `shared/v2_models.py` (`ReviewResult.raw_issue_count`)
- Modify: `backend_code_v2_team/models.py` (`PhaseReviewResult.raw_issue_count`)
- Modify: BE/FE `phases/review.py`, `shared/v2_review.py` (set count when building CR result)
- Modify: `tests/test_shared_llm_review.py` (+ any callers treating return as a bare list)

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class LlmReviewOutput(Generic[IssueT]):
    issues: List[IssueT]
    raw_issue_count: int
```

- `run_llm_review(...) -> LlmReviewOutput[IssueT]`
- Empty input → `LlmReviewOutput([], 0)`
- `raw_issue_count = len(issues)` **before** `drop_ungrounded_issues`

- [ ] **Step 1: Update/add tests** — grounding-on fabrication case asserts `out.raw_issue_count >= 1` and `len(out.issues) < out.raw_issue_count`. Rewrite existing tests to use `.issues`.

- [ ] **Step 2: Run `test_shared_llm_review.py` FAIL**, then implement return type + caller unwraps.

- [ ] **Step 3: Forward into phase review results** where LLM CR builds `PhaseReviewResult` / `ReviewResult`.

- [ ] **Step 4: Run**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/software_engineering_team/tests/test_shared_llm_review.py \
  agents/software_engineering_team/tests/test_v2_review_phase.py \
  agents/software_engineering_team/tests/test_v2_fe_review_phase.py \
  agents/software_engineering_team/tests/test_v2_review_shared.py -v
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: surface pre-grounding raw_issue_count from LLM review"
```

---

### Task 3: CR adapters → `GateOutcome.raw_issue_count`

**Files:**
- Modify: `backend_code_v2_team/phases/execution.py`
- Modify: `frontend_code_v2_team/phases/execution.py`

- [ ] **Step 1: Update CR adapters**

```python
return GateOutcome(
    passed=r.passed,
    issues=r.issues,
    summary=r.summary,
    raw_issue_count=getattr(r, "raw_issue_count", None),
)
```

Leave QA/security adapters unchanged (`raw_issue_count` defaults `None`).

- [ ] **Step 2: Commit**

```bash
git commit -m "feat: copy raw_issue_count into CR GateOutcome"
```

---

### Task 4: Gated loop — streak, trip, dedup (TDD)

**Files:**
- Modify: `shared/phases/execution.py`
- Modify: `tests/test_v2_gated_execution_shared.py`
- Modify: `docs/superpowers/specs/2026-07-15-se-review-loop-grounding-circuit-breaker-design.md` (bad-cycle wording)

**Interfaces:**
- Produces: `_apply_grounding_circuit_breaker_trip(...)` (mirrors retry-exhaust helper)
- Consumes: `cr_call_is_grounding_bad`, `_dedup_issues`, `_record_terminal_gate_failure`

- [ ] **Step 1: Failing tests in `test_v2_gated_execution_shared.py`**

**Hallucination / multi-outer-cycle trip** — script CR so each outer cycle has a failing high-ratio call then a pass; QA fails once per cycle to `continue`; set `grounding_failure_cycle_limit=3`. Assert `REVIEW_FAILED` by cycle 3, `record_gate_outcome` once with `gate="review_grounding_circuit_breaker"`, and `cr.calls` well under what `max_total_cycles` would allow if breaker were absent.

**Low ratio** — failing CR with `raw_issue_count=4`, `len(issues)=3` (ratio 0.25) + batch-fix path that eventually exhausts or passes without trip when limit=3.

**Pass-only high ratio** — CR always passes with `raw_issue_count=4` and empty issues: never trip (passed calls are not bad).

**Dedup** — same `(file_path, description)` on two consecutive batch-fix invocations; second call’s `issues` empty or without the repeat (capture via stub `batch_fix`).

**Disabled** — `grounding_failure_cycle_limit=0` never records circuit-breaker gate.

- [ ] **Step 2: Run FAIL**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/software_engineering_team/tests/test_v2_gated_execution_shared.py -k "grounding or dedup" -v
```

- [ ] **Step 3: Implement loop wiring**

Per microtask before the `while not phase_failed` loop:

```python
grounding_failure_streak = 0
seen_issues: set[tuple[str, str]] = set()
cycle_limit = int(getattr(config, "grounding_failure_cycle_limit", 3))
ratio_threshold = float(getattr(config, "grounding_failure_ratio_threshold", 0.75))
```

At start of each outer cycle CR section: `cycle_bad = False`.

After **every** `cr_outcome = run_code_review_gate(...)` (initial + post-retry):

```python
if cr_call_is_grounding_bad(
    passed=cr_outcome.passed,
    raw_issue_count=cr_outcome.raw_issue_count,
    kept_count=len(cr_outcome.issues),
    ratio_threshold=ratio_threshold,
):
    cycle_bad = True
```

Before each `run_batch_coding_fixes` (CR/QA/security):

```python
issues = _dedup_issues(list(issues), seen_issues)
# skip batch fix if empty after dedup when that matches existing empty-issue behavior
```

When leaving the CR section:

```python
if cycle_limit > 0:
    grounding_failure_streak = grounding_failure_streak + 1 if cycle_bad else 0
if cycle_limit > 0 and grounding_failure_streak >= cycle_limit:
    # trip circuit breaker (helper): REVIEW_FAILED, notes, record, rollback, on_failure
    phase_failed = True
    ...
    break
# else existing not-passed → _apply_code_review_retry_exhausted path
```

- [ ] **Step 4: Amend design doc “Bad cycle” to the clarified rule; run full related suites PASS**

```bash
cd backend && . .venv/bin/activate && python -m pytest \
  agents/software_engineering_team/tests/test_grounding_circuit_breaker.py \
  agents/software_engineering_team/tests/test_v2_gated_execution_shared.py \
  agents/software_engineering_team/tests/test_shared_llm_review.py -v
ruff check agents/software_engineering_team/shared/phases/execution.py
```

- [ ] **Step 5: Commit**

```bash
git commit -m "feat: trip grounding circuit breaker and wire issue dedup in gated loop"
```

---

### Task 5: Verify

- [ ] SE gated + llm_review + circuit_breaker tests green; ruff clean on touched files.
- [ ] Confirm `review_grounding_circuit_breaker` only on breaker path (not write-path / ordinary retry exhaustion).
