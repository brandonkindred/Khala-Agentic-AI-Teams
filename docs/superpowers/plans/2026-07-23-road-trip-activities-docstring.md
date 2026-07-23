# Road Trip Temporal Activities Docstring Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rephrase the module invariant in `road_trip_planning_team/temporal/activities.py` so it names the `job_store` facade activities actually call, matching branding’s docstring style.

**Architecture:** Docstring-only edit. Replace the three-line Invariant paragraph; leave the activity list, sync/import-hygiene, and JSON payload notes untouched. No runtime code changes.

**Tech Stack:** Python module docstring (Sphinx-style double-backticks); ruff via `make lint`.

## Global Constraints

- Work only in `.worktrees/issue-2141-road-trip-activities-docstring` on branch `docs/2141-road-trip-activities-docstring`.
- Touch only `backend/agents/road_trip_planning_team/temporal/activities.py` (module docstring).
- No behavioral changes; no new tests (docstring-only).
- Never reference GitHub issue numbers in code, comments, commit messages, or docs (PR body only later).
- Spec already committed at `docs/superpowers/specs/2026-07-23-road-trip-activities-docstring-design.md` — do not change unless the edit diverges.

## File map

| File | Role |
|------|------|
| `backend/agents/road_trip_planning_team/temporal/activities.py` | Replace Invariant paragraph in module docstring |
| `docs/superpowers/specs/2026-07-23-road-trip-activities-docstring-design.md` | Spec (already committed; reference only) |

---

### Task 1: Rephrase the module Invariant

**Files:**
- Modify: `backend/agents/road_trip_planning_team/temporal/activities.py:25-27`
- Test: none (docstring-only; verify with lint + string check)

**Interfaces:**
- Consumes: none
- Produces: updated module docstring Invariant only

- [ ] **Step 1: Confirm the before text is still present**

Run from the worktree root:

```bash
rg -n "JobServiceClient\` store" backend/agents/road_trip_planning_team/temporal/activities.py
```

Expected: one match at the Invariant lines (currently ~25).

- [ ] **Step 2: Replace the Invariant paragraph**

In `backend/agents/road_trip_planning_team/temporal/activities.py`, replace lines 25–27:

**Before:**

```python
Invariant: job-store status is written to the durable ``JobServiceClient`` store
under the ``road_trip_planning_team`` slug (the same slug the API's ``create_job``
used), so a completed run survives a worker/process restart.
```

**After:**

```python
Invariant: job-store status is written via
``road_trip_planning_team.shared.job_store`` under the
``road_trip_planning_team`` slug — the same slug the API's
``create_job`` used — so a completed run survives a worker/process restart.
```

Do not change any other lines in the module docstring or file body.

- [ ] **Step 3: Verify the new text and that JobServiceClient left the docstring**

```bash
rg -n "road_trip_planning_team\.shared\.job_store|JobServiceClient" \
  backend/agents/road_trip_planning_team/temporal/activities.py
```

Expected:
- One match for `road_trip_planning_team.shared.job_store` in the Invariant.
- Zero matches for `JobServiceClient` in this file.

- [ ] **Step 4: Lint**

```bash
cd backend && LLM_PROVIDER=dummy make lint
```

Expected: exit 0 (ruff clean). Prefer the main-repo venv if the worktree has none:
`/Users/brandonkindred/Documents/GitHub/Khala-Agentic-AI-Teams/backend/.venv`.

- [ ] **Step 5: Commit**

```bash
git add backend/agents/road_trip_planning_team/temporal/activities.py
git commit -m "$(cat <<'EOF'
Clarify road-trip activities docstring uses job_store facade.

Point the module invariant at road_trip_planning_team.shared.job_store
so it matches how Temporal activities persist job status.
EOF
)"
```

---

## Spec coverage (self-review)

| Spec requirement | Task |
|------------------|------|
| Name `road_trip_planning_team.shared.job_store` in Invariant | Task 1 Step 2 |
| Keep slug + restart-survival clause | Task 1 Step 2 (After text) |
| Style parity with branding (facade-first, em-dash slug clause) | Task 1 Step 2 |
| No edits outside activities.py | Global Constraints + File map |
| Lint under dummy LLM | Task 1 Step 4 |
| No placeholders / TBD in plan | Confirmed |
