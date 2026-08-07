# Question-Processing Character-Limit Scope Clarification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Document in `question_processing.py` that question/option text is logged without character truncation, and that `MAX_*` constants are intentional item-count UX caps—not character limits on text fields.

**Architecture:** Documentation-only change in one production module. No runtime behavior, constant values, or call sites change. Regression tests for this finding are deliberately deferred to a follow-on change.

**Tech Stack:** Python 3.10+, Ruff (project lint).

## Global Constraints

- Touch only `backend/agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py`.
- Do not change runtime logic, log format strings, or `MAX_*` / `ANSWER_SIMILARITY_THRESHOLD` values.
- Do not remove or alter `preview[:200]` for malformed LLM JSON logging.
- Do not cite external trackers (issue numbers, PR numbers, or tracker URLs) in the docstring or comments.
- Do not add or update tests in this plan (follow-on work).
- Keep wording short and contract-oriented.

---

## File map

| File | Role |
|---|---|
| `backend/agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py` | Sole modify target: module docstring + `MAX_*` comment block |

---

### Task 1: Document character-limit vs item-count scope

**Files:**
- Modify: `backend/agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py` (module docstring lines 1–10; `MAX_*` comment lines 35–36)
- Test: none (verification via docstring inspection + Ruff only)

**Interfaces:**
- Consumes: existing module docstring and the comment above `MAX_ISSUES` / `MAX_GAPS` / `MAX_OPEN_QUESTIONS`
- Produces: updated docstring and comment text only; no new symbols

- [ ] **Step 1: Replace the module docstring**

Replace the entire top-of-file module docstring (currently ending at line 10) with:

```python
"""
Open-question processing for the Product Requirements Analysis Agent.

Between spec review and asking the user, the raw open questions pass through a
pipeline that parses LLM output into typed :class:`OpenQuestion` models, filters out
duplicates of already-answered questions and organizational/process questions,
consolidates semantically-equivalent questions, checks question/option coherence,
and attaches a recommended option. The LLM-backed steps take an explicit Strands
``model`` and fall back to the unmodified list on any failure; the rest are pure.

Question and option text is logged in full (no character truncation of those
fields). ``MAX_ISSUES``, ``MAX_GAPS``, and ``MAX_OPEN_QUESTIONS`` are intentional
item-count UX caps so a single spec review stays digestible; they are not
character limits on text fields.
"""
```

- [ ] **Step 2: Clarify the `MAX_*` comment block**

Replace the two-line comment immediately above `MAX_ISSUES` (currently lines 35–36) with:

```python
# Item-count UX caps (not character limits on text fields). Chosen to keep a
# single spec review digestible in one sitting rather than tuned against
# measured user drop-off; revisit with product input if that changes.
```

Leave the three constant assignments and the `ANSWER_SIMILARITY_THRESHOLD` block unchanged:

```python
MAX_ISSUES = 10
MAX_GAPS = 10
MAX_OPEN_QUESTIONS = 10
# Empirical SequenceMatcher.ratio() cutoff for "same answer, different wording"
# (shared with software_engineering_team.shared.deduplication's dedupe threshold).
ANSWER_SIMILARITY_THRESHOLD = 0.85
```

- [ ] **Step 3: Verify docstring content without changing behavior**

From the worktree root (or `backend/` with `PYTHONPATH` set as in other agent tests), run:

```bash
cd backend && python -c "
from software_engineering_team.product_requirements_analysis_agent import question_processing as qp
doc = qp.__doc__ or ''
assert 'logged in full' in doc
assert 'item-count UX caps' in doc
assert 'not' in doc.lower() and 'character limits' in doc
assert qp.MAX_ISSUES == 10 and qp.MAX_GAPS == 10 and qp.MAX_OPEN_QUESTIONS == 10
print('ok')
"
```

Expected: prints `ok` and exits 0.

- [ ] **Step 4: Lint the touched file**

```bash
cd backend && ruff check agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py && ruff format --check agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py
```

Expected: both commands exit 0 with no reported issues.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/software_engineering_team/product_requirements_analysis_agent/question_processing.py
git commit -m "$(cat <<'EOF'
Docs: clarify question-processing item-count vs character limits.

State that question/option text is logged in full and that MAX_* caps are
UX item limits, so the scope decision is explicit in the module.
EOF
)"
```

---

## Spec coverage checklist

| Spec requirement | Plan task |
|---|---|
| Module docstring states full-text logging of question/option fields | Task 1 Step 1 |
| Module docstring states `MAX_*` are item-count UX caps, not character limits | Task 1 Step 1 |
| One-line (or short) clarification on the existing `MAX_*` comment | Task 1 Step 2 |
| No runtime behavior / constant value changes | Task 1 Steps 1–3 (assert constants unchanged) |
| Leave `preview[:200]` alone | Global Constraints + no step touches it |
| No tests added in this change | Global Constraints; Task 1 uses inspect + Ruff only |
| Ruff lint for touched file passes | Task 1 Step 4 |
| No external tracker citations in docstring/comments | Global Constraints + Step 1–2 wording |
