# Align `_report_status` Never-Raises Wording Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align `DevOpsTeamLeadAgent._report_status` and `TeamLeadSharedState._report_status` Postconditions so they say “Never raises when preconditions hold,” matching `_log_pipeline_status` and resolving the contradiction with the `phase` assert.

**Architecture:** Documentation-only docstring edits in two files. Keep the existing `assert isinstance(phase, str) and phase` lines; Design-by-Contract treats precondition violations as caller bugs, so “never raises” applies only when preconditions hold.

**Tech Stack:** Python 3.10+ docstrings; pytest for regression verification (no new tests).

**Spec:** `docs/superpowers/specs/2026-07-24-report-status-docstring-design.md`

## Global Constraints

- Docs-only: do not change method bodies, asserts, call sites, or tests.
- Exact replacement phrase: `Never raises when preconditions hold.`
- Do not leave any `Never raises into the caller.` on either `_report_status`.
- Work in worktree `.worktrees/docs-2268-report-status-docstring` on branch `docs/2268-report-status-docstring`.
- Never reference GitHub issue numbers in code, comments, or commit messages (PR body only).

---

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/devops_team/orchestrator.py` | DevOps override docstring Postconditions |
| `backend/agents/software_engineering_team/shared/team_lead_base.py` | Shared base docstring Postconditions |

No new files for the fix itself.

---

### Task 1: Update DevOps `_report_status` docstring

**Files:**
- Modify: `backend/agents/software_engineering_team/devops_team/orchestrator.py` (method `_report_status`, Postconditions only)
- Test: `backend/agents/software_engineering_team/tests/test_devops_status_hook.py` (existing; run only)

**Interfaces:**
- Consumes: none
- Produces: DevOps override Postconditions end with “Never raises when preconditions hold.”

- [ ] **Step 1: Replace the Postconditions closing phrase**

In `DevOpsTeamLeadAgent._report_status`, change only the last sentence of the Postconditions block.

From:

```python
        Postconditions: emits the historical INFO line via ``_log_pipeline_status``;
          then invokes ``TeamLeadSharedState._report_status`` (no-op when callback
          is None; forwards kwargs when set; swallows callback errors). Never
          raises into the caller.
```

To:

```python
        Postconditions: emits the historical INFO line via ``_log_pipeline_status``;
          then invokes ``TeamLeadSharedState._report_status`` (no-op when callback
          is None; forwards kwargs when set; swallows callback errors). Never
          raises when preconditions hold.
```

Do not touch the `assert` or any other lines in the method body.

- [ ] **Step 2: Verify the docstring text**

Run:

```bash
rg -n "Never raises" backend/agents/software_engineering_team/devops_team/orchestrator.py
```

Expected:
- `_log_pipeline_status` still has `Never raises when preconditions hold.`
- `_report_status` now also has `Never raises when preconditions hold.`
- No `Never raises into the caller.` remains in this file.

- [ ] **Step 3: Run existing DevOps status-hook tests**

From `backend/` in the worktree:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_devops_status_hook.py -q
```

Expected: all tests PASS (4 passed).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/devops_team/orchestrator.py
git commit -m "$(cat <<'EOF'
Clarify DevOps _report_status never-raises when preconditions hold.

EOF
)"
```

---

### Task 2: Update shared `TeamLeadSharedState._report_status` docstring

**Files:**
- Modify: `backend/agents/software_engineering_team/shared/team_lead_base.py` (method `_report_status`, Postconditions only)
- Test: `backend/agents/software_engineering_team/tests/test_team_lead_base.py` (existing; run only)

**Interfaces:**
- Consumes: Task 1 wording (same phrase)
- Produces: Base-class Postconditions end with “Never raises when preconditions hold.”

- [ ] **Step 1: Replace the Postconditions closing phrase**

In `TeamLeadSharedState._report_status`, change only the last clause of the Postconditions block.

From:

```python
        Postconditions: if ``_status_callback`` is set, it is invoked once with
          kwargs ``phase``, ``detail``, optional ``progress`` (omitted when
          None), and ``**extra``; callback exceptions are logged and swallowed;
          if the callback is None, this is a no-op. Never raises into the caller.
```

To:

```python
        Postconditions: if ``_status_callback`` is set, it is invoked once with
          kwargs ``phase``, ``detail``, optional ``progress`` (omitted when
          None), and ``**extra``; callback exceptions are logged and swallowed;
          if the callback is None, this is a no-op. Never raises when
          preconditions hold.
```

Do not touch the `assert` or any other lines in the method body.

- [ ] **Step 2: Verify both files**

Run:

```bash
rg -n "Never raises into the caller" \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/shared/team_lead_base.py

rg -n "Never raises when preconditions hold" \
  backend/agents/software_engineering_team/devops_team/orchestrator.py \
  backend/agents/software_engineering_team/shared/team_lead_base.py
```

Expected:
- First command: no matches.
- Second command: hits on `_log_pipeline_status`, DevOps `_report_status`, and base `_report_status` (at least three hits total across the two files).

- [ ] **Step 3: Run existing base status tests**

From `backend/` in the worktree:

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_team_lead_base.py::test_report_status_rejects_empty_phase \
  agents/software_engineering_team/tests/test_team_lead_base.py::test_report_status_swallows_callback_errors \
  agents/software_engineering_team/tests/test_team_lead_base.py::test_report_status_noop_when_callback_unset -q
```

Expected: 3 passed (assert still raises on empty phase; callback errors still swallowed).

- [ ] **Step 4: Commit**

```bash
git add backend/agents/software_engineering_team/shared/team_lead_base.py
git commit -m "$(cat <<'EOF'
Clarify TeamLeadSharedState _report_status never-raises when preconditions hold.

EOF
)"
```

---

## Self-Review

1. **Spec coverage:** Problem (contradiction) → Tasks 1–2. Goal (match sibling wording) → exact phrase in both tasks. Scope B (DevOps + base) → Task 1 + Task 2. Non-goals (no logic/assert/test changes) → Global Constraints + step notes.
2. **Placeholders:** None.
3. **Consistency:** Same replacement phrase in both tasks; asserts left intact.
