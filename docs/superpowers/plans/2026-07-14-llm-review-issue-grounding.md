# LLM Review Issue Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop ungrounded Title Case / quoted claims from `run_llm_review` findings and blank bad file paths, with a config kill switch.

**Architecture:** New `shared/issue_grounding.py`; wire at end of `run_llm_review`; flag on `BaseMicrotaskReviewConfig`; forward through BE/FE `_run_llm_review` and gated code-review path.

**Tech Stack:** Python 3.10, pytest, existing V2 review models.

## Global Constraints

- DbC docstrings on every new public function.
- No GitHub issue numbers in code/comments/commits (PR body only).
- File contents are not grounding sources — only `files.keys()` plus task/architecture text.
- Scan `description` + `recommendation` only.
- Fail-open on grounding exceptions; drop only clear ungrounded phrases.

---

### Task 1: `issue_grounding` module (TDD)

**Files:**
- Create: `backend/agents/software_engineering_team/shared/issue_grounding.py`
- Create: `backend/agents/software_engineering_team/tests/test_issue_grounding.py`

- [x] Write failing unit tests: grounded keep; ungrounded drop; bad path blanked; phrase-free keep; recommendation-only fabrication dropped.
- [x] Implement `extract_checkable_phrases`, `ground_issue_file_path`, `drop_ungrounded_issues`.
- [x] Run `pytest …/test_issue_grounding.py -v` — PASS.

### Task 2: Wire `run_llm_review` + config flag

**Files:**
- Modify: `shared/llm_review.py`, `shared/v2_models.py`
- Modify: `tests/test_shared_llm_review.py`

- [x] Add `enable_llm_review_grounding: bool = True` to `BaseMicrotaskReviewConfig`.
- [x] Add `enable_grounding: bool = True` to `run_llm_review`; when True, call `drop_ungrounded_issues` before return (log drops at WARNING).
- [x] Regression: meal-planning task + insurance-provider finding dropped; `enable_grounding=False` keeps it.
- [x] Existing `test_shared_llm_review.py` still PASS.

### Task 3: Plumb kill switch through BE/FE review

**Files:**
- Modify: `backend_code_v2_team/phases/review.py`, `frontend_code_v2_team/phases/review.py`
- Modify: gated execution / `run_code_review_phase` as needed (closure or optional kwarg)

- [x] `_run_llm_review(..., enable_grounding=True)` forwards to shared helper.
- [x] Code-review gate passes `config.enable_llm_review_grounding` into that path.
- [x] Defaults remain True when config omitted.

### Task 4: Verify

- [x] `pytest backend/agents/software_engineering_team/tests/test_issue_grounding.py backend/agents/software_engineering_team/tests/test_shared_llm_review.py -v`
- [x] Spot-check `test_v2_review_shared.py` if present / relevant.
