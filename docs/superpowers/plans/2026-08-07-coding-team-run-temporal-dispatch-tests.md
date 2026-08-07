# Coding-Team Unconditional Temporal Dispatch Tests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the redundant `is_temporal_enabled`-patched test from `test_coding_team_run_temporal_dispatch.py` so the file reflects Temporal-only dispatch.

**Architecture:** Single-file surgical delete. Production already dispatches unconditionally; the “even when disabled” case only proved that a gate patch does nothing — delete it and keep the remaining three tests.

**Tech Stack:** Python 3.10, pytest, FastAPI `TestClient`.

**Spec:** `docs/superpowers/specs/2026-08-07-coding-team-run-temporal-dispatch-tests-design.md`

**Worktree:** `.worktrees/4004-coding-team-unconditional-dispatch` on branch `feature/4004-coding-team-unconditional-dispatch`

## Global Constraints

- Touch only `backend/agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py`.
- Do not rename remaining tests.
- Do not change production code.
- Do not edit sibling test files.
- Do not reference GitHub issue numbers in code, comments, commit messages, or docs (PR body may use `Closes #N`).
- Prefer exact, minimal diffs.

## File Structure

| File | Responsibility |
|---|---|
| `backend/agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py` | Coding-team `/run` Temporal dispatch unit tests |

No new files.

---

### Task 1: Delete redundant is_temporal_enabled-patched test

**Files:**
- Modify: `backend/agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py`
- Test: same file

**Interfaces:**
- Consumes: existing remaining tests (no API changes)
- Produces: file with no `is_temporal_enabled` references; 3 passing tests

- [ ] **Step 1: Delete the redundant test**

Remove this entire function (and the blank line after it so spacing stays clean between neighboring tests):

```python
def test_run_dispatches_via_temporal_even_when_disabled(monkeypatch):
    """No thread fallback: start_coding_team_workflow is called regardless of is_temporal_enabled()."""
    created: list = []
    monkeypatch.setattr(api, "create_job", lambda **kw: created.append(kw), raising=True)
    monkeypatch.setattr("shared.temporal.is_temporal_enabled", lambda: False)

    dispatched: dict = {}
    monkeypatch.setattr(
        sw,
        "start_coding_team_workflow",
        lambda job_id, repo_path, plan_input: dispatched.update(
            job_id=job_id, repo_path=repo_path, plan_input=plan_input
        ),
    )

    r = client.post("/run", json={"repo_path": "/repo", "plan_input": {"objective": "x"}})

    assert r.status_code == 200
    assert r.json()["status"] == "running"
    assert dispatched["job_id"] == r.json()["job_id"]
    assert len(created) == 1
```

Leave unchanged:
- Module docstring
- `test_run_dispatches_via_temporal_when_enabled`
- `test_run_marks_job_failed_and_503_when_temporal_dispatch_raises`
- `test_run_without_plan_input_creates_row_and_stays_pending`

- [ ] **Step 2: Verify no is_temporal_enabled left and tests pass**

From `backend/`:

```bash
rg -n 'is_temporal_enabled' \
  agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py
```

Expected: no hits.

```bash
/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv/bin/python -m pytest \
  agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py \
  -v
```

Expected: 3 passed (the three remaining tests).

- [ ] **Step 3: Commit**

```bash
git add backend/agents/software_engineering_team/tests/test_coding_team_run_temporal_dispatch.py
git commit -m "$(cat <<'EOF'
Drop redundant is_temporal_enabled patch from coding-team run dispatch tests.

EOF
)"
```

---

## Self-Review

1. **Spec coverage:** Delete even-when-disabled test → Task 1. Grep + pytest verification → Task 1 Step 2. Out-of-scope siblings → Global Constraints.
2. **Placeholder scan:** No TBD; exact deletion target and commands included.
3. **Type consistency:** N/A (test deletion only).
