# Expand Mixed-Case Severity Regression Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand `_reconcile_approval` regression tests so additional mixed-case and whitespace-padded `critical`/`high` variants block, and more non-blocking severities still auto-approve.

**Architecture:** Test-only change. Production gate already folds via `_normalized_severity`. Expand existing parametrize lists in `test_code_review_coordinator.py`; no production edits.

**Tech Stack:** Python 3.10+, pytest, existing `code_review_agent` coordinator tests.

**Spec:** `docs/superpowers/specs/2026-08-06-code-review-mixed-case-severity-regression-tests-design.md`

## Global Constraints

- Production code unchanged (`models.py` / `coordinator.py` not modified).
- Blocking parametrize must include exactly these six tokens (order may match this list): `High`, `HIGH`, ` high `, `Critical`, `CRITICAL`, ` critical `.
- Non-blocking parametrize must include exactly: `Medium`, `LOW`, `Info`.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- Work from worktree `.worktrees/fix-5533-mixed-case-severity-regression` on branch `fix/5533-mixed-case-severity-regression`.
- Pytest via main repo venv:

```bash
WT="/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-5533-mixed-case-severity-regression"
PY="/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python"
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest <paths> -q
```

- `docs/superpowers/` is gitignored — `git add -f` when committing plan/spec files.
- Note: new cases are expected to **pass immediately** (gate already normalized). There is no RED phase for production code; treat this as regression expansion with GREEN verification.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py` | Expand blocking + non-blocking parametrize / rename non-blocking test |

---

### Task 1: Expand blocking + non-blocking parametrize

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py` (around the existing mixed-case reconcile tests)

**Interfaces:**
- Consumes: existing `_issue`, `_reconcile_approval`, `_normalized_severity`
- Produces: broader parametrize coverage only

- [ ] **Step 1: Replace the blocking parametrize and non-blocking test**

Replace this block:

```python
@pytest.mark.parametrize("severity", ["High", "HIGH", " critical "])
def test_reconcile_approval_treats_mixed_case_critical_high_as_blocking(
    severity: str,
) -> None:
    """Blocking membership must match ``_cap_issues`` fold, not raw equality."""
    approved, out = _reconcile_approval(True, [_issue(severity, "blocker")])
    assert approved is False
    assert len(out) == 1
    assert _normalized_severity(out[0].severity) in {"critical", "high"}


def test_reconcile_approval_mixed_case_medium_still_auto_approves() -> None:
    """Non-blocking severities remain non-blocking after case fold."""
    approved, out = _reconcile_approval(False, [_issue("Medium", "nit")])
    assert approved is True
    assert len(out) == 1
```

with:

```python
@pytest.mark.parametrize(
    "severity",
    ["High", "HIGH", " high ", "Critical", "CRITICAL", " critical "],
)
def test_reconcile_approval_treats_mixed_case_critical_high_as_blocking(
    severity: str,
) -> None:
    """Blocking membership must match ``_cap_issues`` fold, not raw equality."""
    approved, out = _reconcile_approval(True, [_issue(severity, "blocker")])
    assert approved is False
    assert len(out) == 1
    assert _normalized_severity(out[0].severity) in {"critical", "high"}


@pytest.mark.parametrize("severity", ["Medium", "LOW", "Info"])
def test_reconcile_approval_mixed_case_non_blocking_still_auto_approves(
    severity: str,
) -> None:
    """Non-blocking severities remain non-blocking after case fold."""
    approved, out = _reconcile_approval(False, [_issue(severity, "nit")])
    assert approved is True
    assert len(out) == 1
```

- [ ] **Step 2: Run expanded + neighbor tests (expect GREEN)**

```bash
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_treats_mixed_case_critical_high_as_blocking \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_mixed_case_non_blocking_still_auto_approves \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_then_reconcile_medium_only_still_approves \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_then_reconcile_keeps_critical_and_rejects \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_normalized_severity_folds_case_and_whitespace -q
```

Expected: all PASS (6 blocking + 3 non-blocking + neighbors). Confirm the old test name `test_reconcile_approval_mixed_case_medium_still_auto_approves` is gone (renamed).

- [ ] **Step 3: Commit**

```bash
cd "$WT"
git add backend/agents/software_engineering_team/tests/test_code_review_coordinator.py
git commit -m "$(cat <<'EOF'
Expand mixed-case severity regression coverage for approval gate.

EOF
)"
```

---

### Task 2: Final verification

**Files:** none new

- [ ] **Step 1: Re-run the same pytest command as Task 1 Step 2**

Expected: all PASS.

- [ ] **Step 2: Confirm no issue-number leakage in the test file**

```bash
cd "$WT"
rg -n '5533|#5533' backend/agents/software_engineering_team/tests/test_code_review_coordinator.py || true
```

Expected: no matches.

- [ ] **Step 3: No commit unless a fix was required**

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| Expand blocking parametrize with six tokens | Task 1 |
| Parametrize non-blocking `Medium`/`LOW`/`Info` | Task 1 |
| Production code unchanged | Global constraints |
| Related coordinator tests pass | Tasks 1–2 |

**Placeholder scan:** none. **Type consistency:** `_issue(severity, ...)` still takes `str`.
