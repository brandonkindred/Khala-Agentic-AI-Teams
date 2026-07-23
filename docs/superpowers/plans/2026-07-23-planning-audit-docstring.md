# Planning Audit Test Docstring Path Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align the `test_planning_audit.py` module docstring with the real import path `software_engineering_team.shared.planning_audit`, leaving `writer.py` unchanged.

**Architecture:** Docs-only one-line edit. Verification already confirmed the module was not relocated to platform `shared`; `planning_team.postgres.writer` already cites the correct SE-team path.

**Tech Stack:** Python docstrings; git worktree at `.worktrees/issue-2139-planning-audit-docstring`.

## Global Constraints

- Do not edit `backend/agents/planning_team/postgres/writer.py`.
- Do not move modules or change imports.
- Do not reference GitHub issue numbers in source, comments, commit messages, or non-PR docs.
- PR body must use `Closes #2139` (PR bodies only).
- Work only in the existing worktree / branch `docs/2139-planning-audit-docstring`.

---

## File Map

| File | Role |
|------|------|
| `backend/agents/software_engineering_team/tests/test_planning_audit.py` | Only file to modify — module docstring line 1 |
| `backend/agents/planning_team/postgres/writer.py` | Do not touch — already correct |
| `docs/superpowers/specs/2026-07-23-planning-audit-docstring-design.md` | Spec (already committed) |

---

### Task 1: Fix test module docstring

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_planning_audit.py:1`
- Do not modify: `backend/agents/planning_team/postgres/writer.py`

**Interfaces:**
- Consumes: None (docs-only)
- Produces: Accurate module docstring citing `software_engineering_team.shared.planning_audit`

- [ ] **Step 1: Replace the module docstring**

In `backend/agents/software_engineering_team/tests/test_planning_audit.py`, change line 1 from:

```python
"""Unit tests for the SE planning_runs audit helper (shared.planning_audit)."""
```

to:

```python
"""Unit tests for the SE planning_runs audit helper (software_engineering_team.shared.planning_audit)."""
```

Leave the rest of the file unchanged (including `from software_engineering_team.shared import planning_audit`).

- [ ] **Step 2: Verify the edit and that writer.py is untouched**

Run from the worktree root:

```bash
head -1 backend/agents/software_engineering_team/tests/test_planning_audit.py
git diff -- backend/agents/planning_team/postgres/writer.py
rg -n "shared\.planning_audit|software_engineering_team\.shared\.planning_audit" \
  backend/agents/software_engineering_team/tests/test_planning_audit.py \
  backend/agents/planning_team/postgres/writer.py
```

Expected:
- `head` shows `software_engineering_team.shared.planning_audit`
- `git diff` for `writer.py` is empty
- `test_planning_audit.py` cites the full SE-team path; `writer.py` still cites `software_engineering_team.shared.planning_audit`

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_planning_audit.py
git commit -m "$(cat <<'EOF'
Clarify planning_audit import path in the SE unit-test docstring.

EOF
)"
```

---

### Task 2: Open PR closing the triage issue

**Files:**
- None (git remote + GitHub only)

**Interfaces:**
- Consumes: Task 1 commit on `docs/2139-planning-audit-docstring`
- Produces: PR URL with `Closes #2139`

- [ ] **Step 1: Push branch**

```bash
git push -u origin HEAD
```

- [ ] **Step 2: Create PR**

```bash
gh pr create --title "Clarify planning_audit path in SE test docstring" --body "$(cat <<'EOF'
## Summary
- Verified `planning_team.postgres.writer` already cites `software_engineering_team.shared.planning_audit` (module was not relocated to platform `shared`).
- Updated the SE `test_planning_audit` module docstring to use that full import path instead of the ambiguous `shared.planning_audit` shorthand.

## Test plan
- [ ] Confirm `head -1 backend/agents/software_engineering_team/tests/test_planning_audit.py` shows the full path
- [ ] Confirm `writer.py` has no diff on this branch

Closes #2139
EOF
)"
```

Expected: `gh` prints a PR URL.

---

## Spec Coverage Self-Review

| Spec requirement | Task |
|------------------|------|
| Leave `writer.py` unchanged | Task 1 Step 2 + Global Constraints |
| Fix test docstring to full path | Task 1 Step 1 |
| Docs-only / no behavior change | Task 1 (docstring only) |
| Close triage via PR keyword | Task 2 Step 2 |
