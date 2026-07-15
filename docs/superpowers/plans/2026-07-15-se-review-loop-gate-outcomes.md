# SE Review Loop Gate Outcomes Wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire `record_gate_outcome` into `run_gated_execution_impl` at terminal `REVIEW_FAILED` sites, after teaching `is_rejected` to understand `GateOutcome.passed`.

**Architecture:** Extend `is_rejected` for `passed`; add `_record_terminal_gate_failure` + `_terminal_failing_outcome` in `execution.py`; call once at code-review retry exhaustion and once at max-cycles.

**Tech Stack:** Python 3.10, pytest, existing `gate_outcomes` + gated-execution test fixtures.

## Global Constraints

- DbC docstrings on new helpers.
- No GitHub issue numbers in code/comments/commits (PR body: `Closes #1277`).
- Observability only — never alter loop control flow or raise from recording.
- Always pass `job_id=""` and `phase="execution"`.
- Do not wire write-path failures or mid-loop QA/security continues.

---

### Task 1: `is_rejected` understands `passed` (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/gate_outcomes.py`
- Modify: `backend/agents/software_engineering_team/tests/test_learnings_ingest.py`

**Interfaces:**
- Produces: `is_rejected(result)` returns `not result.passed` when `passed` is a bool and neither `approved` nor `all_satisfied` is a bool.

- [x] **Step 1: Extend failing tests**

In `test_is_rejected_variants`, add:

```python
assert gate_outcomes.is_rejected(SimpleNamespace(passed=False)) is True
assert gate_outcomes.is_rejected(SimpleNamespace(passed=True)) is False
# approved wins when both present
assert gate_outcomes.is_rejected(SimpleNamespace(approved=True, passed=False)) is False
```

Also add `test_record_gate_outcome_with_passed_false` asserting a `GateOutcome`-shaped `SimpleNamespace(passed=False, summary=..., issues=[...])` records a learning.

- [x] **Step 2: Run to verify FAIL**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_learnings_ingest.py::test_is_rejected_variants -v
```

Expected: FAIL (assert `None is True` or similar).

- [x] **Step 3: Implement**

In `is_rejected`, after `all_satisfied` check:

```python
passed = getattr(result, "passed", None)
if isinstance(passed, bool):
    return not passed
```

Update the docstring/postconditions to mention `passed`.

- [x] **Step 4: Run to verify PASS**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_learnings_ingest.py -v -k "is_rejected or record_gate_outcome"
```

Expected: PASS.

- [x] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/shared/gate_outcomes.py \
  backend/agents/software_engineering_team/tests/test_learnings_ingest.py
git commit -m "fix: teach is_rejected to understand GateOutcome.passed"
```

---

### Task 2: Wire terminal recording into gated loop (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/phases/execution.py`
- Modify: `backend/agents/software_engineering_team/tests/test_v2_gated_execution_shared.py`

**Interfaces:**
- Consumes: `record_gate_outcome(gate, result, *, job_id="", task_id="", phase="execution")`
- Produces:
  - `_record_terminal_gate_failure(gate: str, outcome: Any, task_id: str) -> None`
  - `_terminal_failing_outcome(cr: GateOutcome, qa: GateOutcome, sec: GateOutcome) -> GateOutcome`

- [ ] **Step 1: Write failing wiring tests**

Add to `test_v2_gated_execution_shared.py` (monkeypatch `software_engineering_team.shared.phases.execution.record_gate_outcome` or patch after import — patch the symbol used by the helper; prefer monkeypatching `gate_outcomes.record_gate_outcome` if the helper imports it at call time, or import path on the module).

```python
def test_record_gate_outcome_on_code_review_retry_exhausted(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )
    mt = _microtask()
    _run(
        _make_gate_config(code_review_gate=_fail_gate()),
        [mt],
        tmp_path,
        review_config=_config(cr=1, on_failure="skip_continue"),
    )
    assert mt.status == MS.REVIEW_FAILED
    assert len(calls) == 1
    gate, result, kw = calls[0]
    assert gate == "code_review_retry_exhausted"
    assert kw.get("task_id") == "t1"
    assert kw.get("phase") == "execution"
    assert kw.get("job_id") == ""
    assert result.passed is False


def test_record_gate_outcome_on_max_cycles(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda gate, result, **kw: calls.append((gate, result, kw)) or True,
    )
    # Use existing max-cycles failing fixture pattern from
    # test_max_cycles_guarded_still_failing_review_failed
    ...
    assert calls[0][0] == "review_max_cycles"


def test_record_gate_outcome_not_called_on_success(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    _run(_make_gate_config(), [_microtask()], tmp_path, review_config=_config())
    assert calls == []


def test_record_gate_outcome_not_called_on_qa_recovered(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "software_engineering_team.shared.phases.execution.record_gate_outcome",
        lambda *a, **k: calls.append((a, k)) or True,
    )
    qa = _ScriptedGate([GateOutcome(passed=False, issues=[_issue("qa")], summary="qa")])
    _run(
        _make_gate_config(qa_gate=qa),
        [_microtask()],
        tmp_path,
        review_config=_config(cr=1, qa=2, sec=1),
    )
    assert calls == []
```

Also add a unit-style test for `_terminal_failing_outcome` preference order (cr → qa → sec → synthetic).

- [ ] **Step 2: Run to verify FAIL**

```bash
cd backend && python -m pytest agents/software_engineering_team/tests/test_v2_gated_execution_shared.py -k "record_gate_outcome" -v
```

Expected: FAIL (AttributeError / no calls / import missing).

- [ ] **Step 3: Implement helpers + call sites**

Near `_dedup_issues` in `execution.py`:

```python
from software_engineering_team.shared.gate_outcomes import record_gate_outcome

def _terminal_failing_outcome(
    cr: GateOutcome, qa: GateOutcome, sec: GateOutcome
) -> GateOutcome:
    """Pick the best GateOutcome explaining a max-cycles REVIEW_FAILED.

    Preconditions: all three are GateOutcome instances from the last cycle.
    Postconditions: returns the first with passed=False in order cr→qa→sec;
        otherwise GateOutcome(passed=False, summary=\"Max cycles exceeded\").
    """
    for outcome in (cr, qa, sec):
        if not outcome.passed:
            return outcome
    return GateOutcome(passed=False, summary="Max cycles exceeded")


def _record_terminal_gate_failure(gate: str, outcome: Any, task_id: str) -> None:
    """Best-effort DORA + learning record for a terminal REVIEW_FAILED.

    Preconditions: gate is a non-empty string; outcome is duck-typed for is_rejected.
    Postconditions: calls record_gate_outcome once; never raises; job_id always \"\".
    """
    record_gate_outcome(gate, outcome, job_id="", task_id=task_id, phase="execution")
```

At code-review retry exhaustion (after setting `REVIEW_FAILED`):

```python
_record_terminal_gate_failure("code_review_retry_exhausted", cr_outcome, task_id)
```

At max-cycles `REVIEW_FAILED`:

```python
_record_terminal_gate_failure(
    "review_max_cycles",
    _terminal_failing_outcome(cr_outcome, qa_outcome, sec_outcome),
    task_id,
)
```

- [ ] **Step 4: Run to verify PASS**

```bash
cd backend && python -m pytest \
  agents/software_engineering_team/tests/test_v2_gated_execution_shared.py \
  agents/software_engineering_team/tests/test_learnings_ingest.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/shared/phases/execution.py \
  backend/agents/software_engineering_team/tests/test_v2_gated_execution_shared.py
git commit -m "feat: record gate outcomes on terminal REVIEW_FAILED paths"
```

---

### Task 3: Verify

- [ ] Full related suite green.
- [ ] Reserved gate string `review_grounding_circuit_breaker` documented only in design/plan — not wired yet.
