# Execution Module Docstring Trim Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trim `shared/phases/execution.py`'s module docstring and relocate two unique rationale bits next to their symbols.

**Architecture:** Documentation-only edits in one file per `docs/superpowers/specs/2026-07-15-execution-module-docstring-trim-design.md`. No runtime or test changes.

**Tech Stack:** Python module/class/function docstrings and field comments.

## Global Constraints

- Behavior-preserving: no logic, import, or signature changes.
- Do not mention GitHub issue numbers in source comments or docs beyond this plan's tracking note.
- Keep DbC `Preconditions`/`Postconditions` sections intact where they already exist.

---

## File map

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/shared/phases/execution.py` | Only file modified: module docstring, `run_general_microtask` docstring, `GatedExecutionConfig` gate-adapter field comment |

---

### Task 1: Apply docstring/comment edits

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/phases/execution.py`

- [x] **Step 1: Trim module docstring** to:

```python
"""Shared Execution-phase leaf helpers for the code-v2 teams, including the
gated per-microtask review loop (``run_gated_execution_impl``).
"""
```

- [x] **Step 2: Extend `run_general_microtask` docstring** with StackProfile / `EXECUTION_PROMPT` note (backend has `{language_conventions}`, frontend does not), keeping existing Preconditions/Postconditions.

- [x] **Step 3: Extend gate-adapter field comment** above `run_code_review_gate` to name backend vs frontend adapter shapes and `GateOutcome` normalization.

- [x] **Step 4: Sanity-check** — `python -c` import of the module succeeds; optional smoke: `pytest agents/software_engineering_team/tests/test_v2_gated_execution_shared.py -q --tb=no` from `backend/` if env allows.

- [ ] **Step 5: Commit** with a docs-focused message referencing the maintainability trim (PR body will use `Closes #1346`).
