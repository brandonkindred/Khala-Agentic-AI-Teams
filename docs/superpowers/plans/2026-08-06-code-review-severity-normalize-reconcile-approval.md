# Normalize Code-Review Severity Blocking Checks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make critical/high blocking checks in `_reconcile_approval` and `ChunkReviewLLMResponse` case-insensitive and whitespace-tolerant, sharing the same fold used by `_cap_issues`.

**Architecture:** Add `_normalized_severity` next to `CodeReviewIssueSeverity` in `models.py`. Use it for `_cap_issues` rank keys, `_reconcile_approval` blocking membership, and the LLM response consistency validator. Normalize at compare time only — do not mutate stored severity strings.

**Tech Stack:** Python 3.10+, pytest, Pydantic v2, existing `code_review_agent` coordinator/models tests.

**Spec:** `docs/superpowers/specs/2026-08-06-code-review-severity-normalize-reconcile-approval-design.md`

## Global Constraints

- Blocking set remains exactly `{"critical", "high"}` after `(severity or "").strip().lower()`.
- Do not change severity taxonomies, UI display, or `security_service.is_blocking`.
- Do not mutate `CodeReviewIssue.severity` / LLM issue severity values — fold only at compare/rank time.
- No GitHub issue numbers in code, comments, commit messages, or docs (PR body only).
- Design-by-Contract: `Preconditions:` / `Postconditions:` on `_normalized_severity`; keep `_reconcile_approval` postcondition (`approved is False` ⇒ ≥1 critical/high after normalization).
- ≥90% line coverage on changed Python; touched pytest suites must pass.
- Work from worktree `.worktrees/fix-5532-normalize-severity-reconcile-approval` on branch `fix/5532-normalize-severity-reconcile-approval`.
- Pytest via main repo venv (worktree has no local `.venv`):

```bash
WT="/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/.worktrees/fix-5532-normalize-severity-reconcile-approval"
PY="/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python"
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest <paths> -q
```

- `docs/superpowers/` is gitignored — `git add -f` when committing plan/spec files.

## File map

| Path | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/code_review_agent/models.py` | Own `_normalized_severity`; use it in `ChunkReviewLLMResponse` validator |
| `backend/agents/software_engineering_team/code_review_agent/coordinator.py` | Import helper; use in `_cap_issues` + `_reconcile_approval` |
| `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py` | Helper + reconcile mixed-case / whitespace tests |
| `backend/agents/software_engineering_team/tests/test_chunk_review_llm_schema.py` | Confirm validator still consistent; document Literal boundary for mixed-case |

**Note on LLM schema:** `ChunkReviewIssueLLM.severity` is `CodeReviewIssueSeverity` (`Literal` of lowercase tokens). Mixed-case `"HIGH"` fails **field** validation before the after-validator. The validator still uses `_normalized_severity` for defense-in-depth and parity with `CodeReviewIssue` (free `str`). Mixed-case blocking is proven on the coordinator path; helper unit tests cover the fold itself.

---

### Task 1: `_normalized_severity` helper (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/models.py` (insert immediately after `CodeReviewIssueSeverity = Literal[...]`)
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py` (imports + new tests near the existing `_issue` / cap-reconcile block)

**Interfaces:**
- Consumes: optional severity string
- Produces:

```python
def _normalized_severity(severity: Optional[str]) -> str:
    """Fold a severity token for rank / blocking comparisons.

    Preconditions:
        - ``severity`` is ``None`` or a string (may be empty / padded / mixed-case).

    Postconditions:
        - Returns ``(severity or "").strip().lower()``.
        - Never raises; empty / ``None`` → ``""``.
        - Pure; no side effects.
    """
```

- [ ] **Step 1: Write the failing tests**

In `test_code_review_coordinator.py`, add to the coordinator import list:

```python
from code_review_agent.models import (
    ChunkReviewOutput,
    CodeReviewInput,
    CodeReviewIssue,
    CodeReviewOutput,
    CodeReviewUnavailableError,
    FileSegment,
    ReviewChunk,
    _normalized_severity,
    is_no_op_suggestion,
)
```

(Keep existing `from code_review_agent.models import ...` consolidations — merge `_normalized_severity` into the existing models import block rather than duplicating it.)

Add tests after `_issue`:

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, ""),
        ("", ""),
        ("high", "high"),
        ("High", "high"),
        ("HIGH", "high"),
        (" critical ", "critical"),
        ("Medium", "medium"),
    ],
)
def test_normalized_severity_folds_case_and_whitespace(raw: str | None, expected: str) -> None:
    assert _normalized_severity(raw) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_normalized_severity_folds_case_and_whitespace -q
```

Expected: FAIL with `ImportError` / `cannot import name '_normalized_severity'`.

- [ ] **Step 3: Implement the helper**

In `models.py`, immediately after `CodeReviewIssueSeverity = Literal["critical", "high", "medium", "low", "info"]`, add:

```python
def _normalized_severity(severity: Optional[str]) -> str:
    """Fold a severity token for rank / blocking comparisons.

    Preconditions:
        - ``severity`` is ``None`` or a string (may be empty / padded / mixed-case).

    Postconditions:
        - Returns ``(severity or "").strip().lower()``.
        - Never raises; empty / ``None`` → ``""``.
        - Pure; no side effects.
    """
    return (severity or "").strip().lower()
```

Ensure `Optional` is already imported from `typing` in this file (it is).

- [ ] **Step 4: Run tests to verify they pass**

Same pytest command as Step 2. Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd "$WT"
git add backend/agents/software_engineering_team/code_review_agent/models.py \
  backend/agents/software_engineering_team/tests/test_code_review_coordinator.py
git commit -m "$(cat <<'EOF'
Add shared severity fold for code-review blocking checks.

EOF
)"
```

---

### Task 2: Wire `_reconcile_approval` + `_cap_issues` (TDD)

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/coordinator.py` (import + `_cap_issues` key + `_reconcile_approval` filter)
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_coordinator.py`

**Interfaces:**
- Consumes: `_normalized_severity` from Task 1
- Produces: case-insensitive blocking membership in `_reconcile_approval`; identical ranking behavior in `_cap_issues` via the shared helper

- [ ] **Step 1: Write the failing tests**

Add after the existing `test_cap_then_reconcile_keeps_critical_and_rejects`:

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

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_treats_mixed_case_critical_high_as_blocking \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_mixed_case_medium_still_auto_approves -q
```

Expected: FAIL — `test_reconcile_approval_treats_mixed_case_critical_high_as_blocking` asserts `approved is False` but gets `True` (current case-sensitive filter). Medium case may already pass; that is fine.

- [ ] **Step 3: Wire coordinator**

1. Add `_normalized_severity` to the existing `from .models import (...)` block.

2. In `_cap_issues`, replace the inline fold:

```python
            _CAP_SEVERITY_RANK.get(
                _normalized_severity(pair[1].severity), _CAP_UNKNOWN_SEVERITY_RANK
            ),
```

3. In `_reconcile_approval`, replace the membership line:

```python
    critical_or_high = [
        i for i in issues if _normalized_severity(i.severity) in ("critical", "high")
    ]
```

Leave the rest of `_reconcile_approval` unchanged (logging, auto-approve path, return).

- [ ] **Step 4: Run targeted + regression tests**

```bash
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_normalized_severity_folds_case_and_whitespace \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_treats_mixed_case_critical_high_as_blocking \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_mixed_case_medium_still_auto_approves \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_issues_under_limit_preserves_order \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_issues_exactly_at_limit_preserves_order \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_issues_over_limit_severity_first_stable_within_rank \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_then_reconcile_medium_only_still_approves \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_then_reconcile_keeps_critical_and_rejects -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd "$WT"
git add backend/agents/software_engineering_team/code_review_agent/coordinator.py \
  backend/agents/software_engineering_team/tests/test_code_review_coordinator.py
git commit -m "$(cat <<'EOF'
Normalize severity when reconciling code-review approval.

EOF
)"
```

---

### Task 3: Wire `ChunkReviewLLMResponse` validator

**Files:**
- Modify: `backend/agents/software_engineering_team/code_review_agent/models.py` (`_require_approval_consistent_with_issues`)
- Modify: `backend/agents/software_engineering_team/tests/test_chunk_review_llm_schema.py`

**Interfaces:**
- Consumes: `_normalized_severity` from Task 1 (same module — no new import)
- Produces: validator severity membership uses the same fold as `_reconcile_approval`

- [ ] **Step 1: Write the tests**

In `test_chunk_review_llm_schema.py`, update the models import:

```python
from code_review_agent.models import (
    ChunkReviewIssueLLM,
    ChunkReviewLLMResponse,
    _normalized_severity,
)
```

Add:

```python
def test_mixed_case_severity_is_rejected_by_issue_literal_before_consistency_check() -> None:
    """ChunkReviewIssueLLM.severity is a lowercase Literal, so mixed-case never
    reaches the after-validator. Coordinator ``CodeReviewIssue`` (free str) is
    where case-insensitive blocking matters; this locks the schema boundary."""
    with pytest.raises(ValidationError):
        ChunkReviewIssueLLM.model_validate(
            {"severity": "HIGH", "description": "SQL injection risk"}
        )


def test_normalized_severity_helper_matches_blocking_fold_used_by_validator() -> None:
    """Guard the shared fold the consistency check relies on."""
    assert _normalized_severity("HIGH") == "high"
    assert _normalized_severity(" critical ") == "critical"
```

Keep existing `test_approved_false_with_a_populated_high_issue_is_accepted` and
`test_approved_true_with_an_actionable_critical_issue_is_rejected` as regressions.

- [ ] **Step 2: Run new tests (helper already exists; Literal test should pass; proceed to wire)**

```bash
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest \
  agents/software_engineering_team/tests/test_chunk_review_llm_schema.py::test_mixed_case_severity_is_rejected_by_issue_literal_before_consistency_check \
  agents/software_engineering_team/tests/test_chunk_review_llm_schema.py::test_normalized_severity_helper_matches_blocking_fold_used_by_validator -q
```

Expected: PASS (helper exists; Literal already rejects `HIGH`).

- [ ] **Step 3: Update the validator**

In `ChunkReviewLLMResponse._require_approval_consistent_with_issues`, replace:

```python
        has_actionable_critical_or_high = any(
            issue.severity in ("critical", "high")
            and issue.description.strip()
            and not is_no_op_suggestion(issue.suggestion)
            for issue in self.issues
        )
```

with:

```python
        has_actionable_critical_or_high = any(
            _normalized_severity(issue.severity) in ("critical", "high")
            and issue.description.strip()
            and not is_no_op_suggestion(issue.suggestion)
            for issue in self.issues
        )
```

Optionally tighten the method docstring's "severity in critical/high" phrase to say "normalized severity in critical/high" (one sentence; keep DbC clarity).

- [ ] **Step 4: Run schema suite regressions**

```bash
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest \
  agents/software_engineering_team/tests/test_chunk_review_llm_schema.py -q
```

Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
cd "$WT"
git add backend/agents/software_engineering_team/code_review_agent/models.py \
  backend/agents/software_engineering_team/tests/test_chunk_review_llm_schema.py
git commit -m "$(cat <<'EOF'
Use shared severity fold in chunk-review approval consistency check.

EOF
)"
```

---

### Task 4: Final verification

**Files:** none new

- [ ] **Step 1: Run combined verification**

```bash
cd "$WT/backend" && PYTHONPATH=agents:agents/software_engineering_team:. "$PY" -m pytest \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_normalized_severity_folds_case_and_whitespace \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_treats_mixed_case_critical_high_as_blocking \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_reconcile_approval_mixed_case_medium_still_auto_approves \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_then_reconcile_medium_only_still_approves \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_then_reconcile_keeps_critical_and_rejects \
  agents/software_engineering_team/tests/test_code_review_coordinator.py::test_cap_issues_over_limit_severity_first_stable_within_rank \
  agents/software_engineering_team/tests/test_chunk_review_llm_schema.py -q
```

Expected: all PASS.

- [ ] **Step 2: Confirm no issue-number leakage**

```bash
cd "$WT"
rg -n '5532|#5532' \
  backend/agents/software_engineering_team/code_review_agent/models.py \
  backend/agents/software_engineering_team/code_review_agent/coordinator.py \
  backend/agents/software_engineering_team/tests/test_code_review_coordinator.py \
  backend/agents/software_engineering_team/tests/test_chunk_review_llm_schema.py \
  || true
```

Expected: no matches in those implementation/test files.

- [ ] **Step 3: No extra commit unless verification required a fix** — if a fix was needed, commit it with a message describing the fix (still no issue numbers).

---

## Spec coverage (self-review)

| Spec requirement | Task |
|---|---|
| `_normalized_severity` helper with DbC | Task 1 |
| `_cap_issues` uses helper | Task 2 |
| `_reconcile_approval` normalized membership | Task 2 |
| `ChunkReviewLLMResponse` uses same fold | Task 3 |
| Mixed-case / whitespace blocking tests | Task 2 (+ helper Task 1) |
| Medium mixed-case still non-blocking | Task 2 |
| Postcondition preserved | Task 2 (behavior unchanged for lowercase; auto-approve path untouched) |
| No taxonomy / UI / `is_blocking` changes | Global constraints |
| Normalize at compare time only | Global constraints + implementations |

**Placeholder scan:** none. **Type consistency:** `_normalized_severity(Optional[str]) -> str` used identically in coordinator and validator.
