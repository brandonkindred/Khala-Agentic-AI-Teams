# Question-Processing Scope Regression Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add regression tests that lock the `question_processing` module docstring and `MAX_*` comment contract distinguishing full-text logging and item-count UX caps from character limits on text fields.

**Architecture:** Two focused tests in the existing product-requirements analysis test module. One asserts `__doc__` phrases; one asserts the source comment near `MAX_ISSUES`. No production code changes. Whitespace is normalized with `" ".join(...split())` to match SE suite docstring checks.

**Tech Stack:** Python 3.10+, pytest, pathlib.

## Global Constraints

- Touch only `backend/agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py` for the functional change (plus plan/spec docs already committed if present).
- Do not modify `question_processing.py` or any other production module.
- Do not add caplog / logging-behavior tests.
- Do not cite external trackers in test names, docstrings, or comments.
- Keep tests short; assert stable substrings from the approved clarification wording.
- Unrelated test refactors are out of scope.

---

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py` | Add two regression tests near the existing `cap_open_questions` / `MAX_*` tests |

---

### Task 1: Add docstring and MAX_* comment regression tests

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py` (insert after `test_cap_open_questions_preserves_order_and_limit`, around line 789)
- Test: same file (the new tests are the deliverable)

**Interfaces:**
- Consumes: `product_requirements_analysis_agent.question_processing` module (`__doc__`, `__file__`, `MAX_ISSUES` presence in source)
- Produces: `test_question_processing_module_doc_states_full_text_logging_and_item_count_caps`, `test_question_processing_max_constants_comment_states_item_count_not_character_limits`

- [ ] **Step 1: Write the two regression tests**

Insert the following immediately after `test_cap_open_questions_preserves_order_and_limit` (after its final assert / blank line, before the next test function):

```python
def test_question_processing_module_doc_states_full_text_logging_and_item_count_caps() -> None:
    """Module docstring locks full-text logging and item-count (not char) caps."""
    import product_requirements_analysis_agent.question_processing as qp

    doc = " ".join((qp.__doc__ or "").split()).lower()
    assert "logged in full" in doc
    assert "no character truncation" in doc
    assert "item-count ux caps" in doc
    assert "not character limits on text fields" in doc


def test_question_processing_max_constants_comment_states_item_count_not_character_limits() -> None:
    """Source comment above MAX_* states item-count caps, not character limits."""
    from pathlib import Path

    import product_requirements_analysis_agent.question_processing as qp

    source = Path(qp.__file__).read_text(encoding="utf-8")
    # Narrow to the block that introduces MAX_ISSUES so unrelated later
    # mentions of "character" (e.g. stem helpers) cannot satisfy the assert.
    marker = "MAX_ISSUES = 10"
    idx = source.index(marker)
    preamble = source[max(0, idx - 400) : idx]
    comment = " ".join(preamble.split()).lower()
    assert "item-count ux caps" in comment
    assert "not character limits on text fields" in comment
```

If `Path` is already imported at module top (it is: `from pathlib import Path`), omit the inner `from pathlib import Path` and use the module-level `Path`.

- [ ] **Step 2: Confirm the tests would fail without the clarification (RED feasibility)**

Without mutating the working tree, verify the parent-of-fix tip lacks the phrases:

```bash
cd backend && git show 'f3a1c38bd^:agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py' | head -50
```

Expected: the pre-fix module docstring ends after the "pure." paragraph and the comment above `MAX_ISSUES` does **not** contain `item-count UX caps` / `not character limits on text fields`.

- [ ] **Step 3: Run the new tests (GREEN — clarification already on this branch)**

```bash
cd backend && PYTHONPATH=agents:.. python -m pytest \
  agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py::test_question_processing_module_doc_states_full_text_logging_and_item_count_caps \
  agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py::test_question_processing_max_constants_comment_states_item_count_not_character_limits \
  -v
```

Expected: both PASS.

- [ ] **Step 4: Run the related package test file**

```bash
cd backend && PYTHONPATH=agents:.. python -m pytest \
  agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py -q --tb=line
```

Expected: all collected tests PASS (224 prior + 2 new).

- [ ] **Step 5: Lint the touched test file**

```bash
cd backend && ruff check agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py \
  && ruff format --check agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py
```

Expected: both exit 0. If format wants changes, run `ruff format` on that file and re-check.

- [ ] **Step 6: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_product_requirements_analysis_agent.py
git commit -m "$(cat <<'EOF'
Test: lock question-processing scope docstring and MAX_* comment.

Regression coverage for full-text logging wording and item-count UX
caps (not character limits) in the module contract.
EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Plan task |
|---|---|
| Assert `__doc__` full-text logging + item-count UX caps wording | Task 1 Step 1 (first test) |
| Assert source comment near `MAX_ISSUES` | Task 1 Step 1 (second test) |
| Tests would fail if clarification removed | Task 1 Step 2 (RED feasibility) + Step 3 (GREEN) |
| No production code changes | Global Constraints |
| No caplog tests | Global Constraints |
| Related package tests still pass | Task 1 Step 4 |
| Ruff for touched file passes | Task 1 Step 5 |
| No external tracker citations | Global Constraints + test names/docstrings |
