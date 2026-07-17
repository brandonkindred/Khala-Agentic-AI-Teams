# Strategy Lab `is_publishable` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Gate Strategy Lab paper-trading on a new persisted `is_publishable` flag derived from existing robustness gates, without changing `is_winning` semantics.

**Architecture:** Compute `is_publishable` in `_run_verification_phase` after publication vetoes; persist on `StrategyLabRecord`; both paper-trade entry points gate on it. Shared `publishability_skip_reason(...)` helper joins failing gate codes in veto order.

**Tech Stack:** Python 3.10, Pydantic models, pytest, existing Strategy Lab orchestrator + `api/main.py` finalize path.

## Global Constraints

- DbC docstrings on new/changed public helpers (`Preconditions` / `Postconditions` / `Invariants` where relevant).
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only: `Closes #1564`).
- Do not change the 8% `WINNING_THRESHOLD` math or paper-trading engine internals.
- Do not flip `is_winning` from vetoes — caveats on `acceptance_reason` only.
- Legacy / missing `is_publishable` deserializes to `False`.
- Backend coverage ≥90% on touched code.

## File map

| File | Responsibility |
|---|---|
| `strategy_lab/_orchestrator_helpers.py` | `publishability_skip_reason` helper + `_VerificationOutcome.is_publishable` |
| `strategy_lab/orchestrator.py` | Compute + thread `is_publishable` into assembled record |
| `models.py` | `StrategyLabRecord.is_publishable: bool = False` |
| `api/main.py` | Integrated finalize + standalone POST gates |
| `system_design/strategy_lab_pipeline.md` | Document winner vs publishable |
| `system_design/paper_trading_integration.md` | Same contract |
| `quality_gates/cost_stress_realism.py` | Fix false "vetoes is_winning" docstring |
| `quality_gates/realism/__init__.py` | Same docstring fix |
| `tests/test_publishability.py` (new) | Unit tests for helper + verification/finalize behavior |
| `tests/test_realism_orchestrator_wiring.py` | Extend caveat tests for `is_publishable` |
| `tests/test_investment_team.py` | Extend paper-trade skip path tests |

---

### Task 1: `publishability_skip_reason` helper + model field (TDD)

**Files:**
- Create: `backend/agents/investment_team/tests/test_publishability.py`
- Modify: `backend/agents/investment_team/strategy_lab/_orchestrator_helpers.py`
- Modify: `backend/agents/investment_team/models.py` (`StrategyLabRecord`)

**Interfaces:**
- Produces:
  ```python
  def publishability_skip_reason(
      *,
      exit_rule_conformance_passed: bool,
      realism_passed: bool,
      trades_aligned: bool,
      runtime_lookahead_violation: bool,
  ) -> Optional[str]:
      """Return comma-joined failing gate codes in veto order, or None if all pass."""
  ```
  Codes (fixed order): `exit_rule_conformance_failed`, `realism_failed`, `alignment_unresolved`, `lookahead_violation`.
- Produces: `StrategyLabRecord.is_publishable: bool = False`
- Produces: `_VerificationOutcome.is_publishable: bool`

- [ ] **Step 1: Write failing tests**

```python
from investment_team.strategy_lab._orchestrator_helpers import publishability_skip_reason
from investment_team.models import StrategyLabRecord

def test_publishability_skip_reason_none_when_all_pass():
    assert publishability_skip_reason(
        exit_rule_conformance_passed=True,
        realism_passed=True,
        trades_aligned=True,
        runtime_lookahead_violation=False,
    ) is None

def test_publishability_skip_reason_joins_in_veto_order():
    assert publishability_skip_reason(
        exit_rule_conformance_passed=False,
        realism_passed=False,
        trades_aligned=False,
        runtime_lookahead_violation=True,
    ) == (
        "exit_rule_conformance_failed,realism_failed,"
        "alignment_unresolved,lookahead_violation"
    )

def test_strategy_lab_record_is_publishable_defaults_false():
    # Build minimal record the same way other tests do; omit is_publishable.
    # Assert record.is_publishable is False and model_validate({"...", without field}) works.
    ...
```

- [ ] **Step 2: Run to verify FAIL**

```bash
cd backend && python -m pytest agents/investment_team/tests/test_publishability.py -v
```

Expected: FAIL (import / attribute errors).

- [ ] **Step 3: Implement helper + model + outcome field**

- [ ] **Step 4: Run to verify PASS**

- [ ] **Step 5: Commit** (only if user requested commits; otherwise skip)

---

### Task 2: Orchestrator computes and persists `is_publishable` (TDD)

**Files:**
- Modify: `strategy_lab/orchestrator.py` (`_run_verification_phase`, `_orchestrate_verification_and_analysis`, `_extract_findings_and_assemble_record`, `_assemble_record`, early-exit record sites)
- Modify: `tests/test_realism_orchestrator_wiring.py`
- Modify: `tests/test_publishability.py`

**Interfaces:**
- After `_apply_publication_vetoes`, set:
  ```python
  is_publishable = bool(
      is_winning
      and realism_passed
      and trades_aligned
      and exit_rule_conformance_passed
      and not runtime_lookahead_violation
  )
  ```
- Thread `is_publishable` through to `StrategyLabRecord(...)`.
- Emit `is_publishable` on the `complete` phase payload alongside `is_winning`.

- [ ] **Step 1: Extend failing tests** — realism critical → `is_winning=True`, `is_publishable=False`; clean path → both True; misaligned → `is_publishable=False` with alignment code when tested via helper/verification.

- [ ] **Step 2: Run FAIL → implement → PASS**

---

### Task 3: Paper-trade gates (integrated + standalone) (TDD)

**Files:**
- Modify: `api/main.py` (`_finalize_strategy_lab_cycle_record`, `POST /strategy-lab/paper-trade`)
- Modify: `tests/test_investment_team.py` (paper-trading skip cases)
- Modify: `tests/test_publishability.py` (API-level cases as needed)

**Interfaces:**
- Finalize order:
  1. `not is_winning` → `not_winning`
  2. `is_winning and not is_publishable` → `publishability_skip_reason(...)` **or** if gate booleans are not on the record, use a reason already on the record / recompute only from what we persist
- **Chosen:** Persist only `is_publishable` on the record. At finalize time inside the cycle, the orchestrator just finished — but finalize receives the record only. So either:
  - (A) Persist `publishability_failed_reason: Optional[str]` on the record when not publishable, set during `_assemble_record`, OR
  - (B) Recompute skip reason in finalize from acceptance_reason suffixes (fragile — rejected in design).

**Lock (A):** On assemble, when `not is_publishable`, set nothing yet on `paper_trading_*`; instead store optional field `publishability_skip_reason: Optional[str] = None` on `StrategyLabRecord` filled at assemble from the helper. Finalize copies it into `paper_trading_skipped_reason` when skipping. Standalone 400 uses `record.publishability_skip_reason` or generic text.

Actually the approved design said: prefer shared helper; standalone uses `paper_trading_skipped_reason` when already set. For the **first** skip in finalize, finalize must call the helper — so the gate booleans must be available at finalize OR the skip reason must already be on the record.

**Simplest lock:** Persist `publishability_skip_reason: Optional[str] = None` on `StrategyLabRecord` (set in `_assemble_record` via helper; `None` when publishable). Finalize:

```python
if not record.is_winning:
    ... not_winning
elif not record.is_publishable:
    reason = record.publishability_skip_reason or "not_publishable"
    record.paper_trading_status = "skipped"
    record.paper_trading_skipped_reason = reason
```

Standalone:

```python
if not lab_record.is_winning: ... existing 400
if not lab_record.is_publishable:
    raise HTTPException(400, detail=... reason from publishability_skip_reason or generic)
```

- [ ] **Step 1: Failing tests** for winning+not publishable skip; clean winner still paper-trades; standalone 400; legacy default False rejects.

- [ ] **Step 2–4: FAIL → implement → PASS**

---

### Task 4: Docs + docstring fixes

**Files:**
- `system_design/strategy_lab_pipeline.md`
- `system_design/paper_trading_integration.md`
- `quality_gates/cost_stress_realism.py`
- `quality_gates/realism/__init__.py`

- [ ] Update winner vs publishable sections, mermaid, skip-reason table.
- [ ] Fix "veto `is_winning`" → contribute to `is_publishable` / stamp `acceptance_reason`.

---

### Task 5: Full verification

```bash
cd backend && python -m pytest agents/investment_team/tests/test_publishability.py \
  agents/investment_team/tests/test_realism_orchestrator_wiring.py \
  agents/investment_team/tests/test_investment_team.py -k "paper_trad or publishab or realism" -v
```

Coverage check on touched modules if CI requires.

---

## Spec coverage checklist

| Spec requirement | Task |
|---|---|
| `is_publishable` formula in orchestrator | 2 |
| Both paper-trade entry points gate on it | 3 |
| Winning + not publishable persists flags + skip reason | 2+3 |
| Docs in `strategy_lab_pipeline.md` | 4 |
| Fix `cost_stress_realism.py` docstring | 4 |
| Tests: cost stress, misaligned, clean winner | 2+3 |
| Legacy default False | 1+3 |
| Joined gate codes in veto order | 1 |
| Do not change 8% / engine | Global |
