# Production Review Agents In-Process Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `CodeReviewAgent(force_in_process=True)` skip Temporal at `run()` time, and wire that into `build_production_review_kwargs_in_process`.

**Architecture:** Instance flag on `CodeReviewAgent`; Temporal activity helper passes `force_in_process=True` and stops mutating `TEMPORAL_ADDRESS`. Thread-mode helper unchanged.

**Tech Stack:** Python 3.10, pytest, existing `code_review_agent` Temporal dispatch.

## Global Constraints

- Default remains Temporal-first (`force_in_process=False`).
- Helpers never raise; degrade to `{}` on construction failure.
- PR must include `Closes #1273`.
- Never reference GitHub issues in source/comments (PR body only).

---

## File map

| File | Change |
|---|---|
| `code_review_agent/agent.py` | Add `force_in_process`; skip Temporal in `run()` |
| `shared/production_review_agents.py` | Pass flag; remove env mutation |
| `tests/test_code_review_temporal.py` or `tests/test_code_review_agent.py` | Force-flag regression |
| `tests/test_production_review_agents.py` | Update in-process helper tests |

---

### Task 1: Failing force_in_process test

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_code_review_temporal.py`

- [ ] Write test: with temporal enabled patched True, `CodeReviewAgent(..., force_in_process=True).run(...)` calls `run_coordinator`, never `_run_via_temporal`
- [ ] Run test; confirm RED (flag missing / Temporal still called)
- [ ] Implement `force_in_process` on `CodeReviewAgent`
- [ ] Run test; confirm GREEN

### Task 2: Fix production helper

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/production_review_agents.py`
- Modify: `backend/agents/software_engineering_team/tests/test_production_review_agents.py`

- [ ] Update helper to `CodeReviewAgent(..., force_in_process=True)`; remove env dance
- [ ] Replace env-restore tests with “does not mutate TEMPORAL_ADDRESS” + asserts constructor kwargs
- [ ] Run `test_production_review_agents.py` + force-flag test; all green

### Task 3: Commit and PR

- [ ] Commit implementation
- [ ] Push branch and `gh pr create` with `Closes #1273`
