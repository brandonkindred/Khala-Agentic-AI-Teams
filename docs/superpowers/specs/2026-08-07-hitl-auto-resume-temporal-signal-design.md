# Design: HITL auto-resume signals CodingTeamWorkflow

Date: 2026-08-07

## Goal

Update `_try_auto_resume` so Temporal-native paused jobs (those with a
`resume_token` on the job record) wake the running `CodingTeamWorkflow` via
the `submit_answers` signal — the same contract as `POST /run/{job_id}/resume`
— instead of acquiring a resume claim and spawning a thread.

Automatic resume after answers (including the deferred recheck timer) becomes
durable through Temporal when the job is Temporal-native. Thread-mode
claim+spawn stays until sibling cleanup issues remove it.

## Context

Part of the coding-team HITL Temporal redesign. The manual `/resume` Temporal
path is already on `main`: after terminal / `waiting_for_user` checks, a
truthy `resume_token` calls `signal_workflow_sync` and skips claim+spawn.

`POST /run/{job_id}/answers` already mode-branches on `resume_token` and
signals directly before ever reaching `_try_auto_resume`. Thread-mode
`/answers` still calls `_try_auto_resume` when the local run thread is dead.
`_schedule_resume_recheck` also calls `_try_auto_resume` after a deferred
liveness window.

Without a Temporal branch inside `_try_auto_resume`, any caller that reaches
it with a `resume_token` job would still claim+spawn — wrong for a durable
workflow wait. Mode-branching the helper closes that gap for all callers.

Out of scope: changing the manual `/resume` route, extracting a shared helper
with `/answers`/`/resume`, deleting claim/heartbeat machinery.

## Decisions

| Topic | Choice |
|---|---|
| Where to branch | Inside `_try_auto_resume` (orchestration.py), after terminal + `waiting_for_user` checks |
| Temporal action | `signal_workflow_sync` with signal name `submit_answers` |
| Workflow id | `f"{WORKFLOW_ID_PREFIX}{job_id}"` (`WORKFLOW_ID_PREFIX = "coding_team-"`) |
| Signal payload | `{"resume_token": <job.resume_token>, "answers": <job.submitted_answers or []>}` — same as `/resume` |
| Signal failure | Catch any exception, log, return `False` (preserve `_try_auto_resume`'s never-raises contract) |
| Success return | `True` (same meaning as "resuming" for callers) |
| Skipped on Temporal path | Heartbeat deferral / recheck scheduling, plan recovery, GitHub token resolution, claim+spawn |
| Thread-mode path | Unchanged after the Temporal early return |
| Shared helper with `/resume` | Not extracted in this change |
| `/answers` Temporal path | Unchanged (already signals inline) |

## Behavior

`_try_auto_resume(job_id, data)` in `api/orchestration.py`:

1. If `hitl.is_terminal(data)` → `False` (unchanged).
2. If `data.get("status") != hitl.WAITING_STATUS` → `False` (unchanged).
3. If `data.get("resume_token")` is truthy:
   1. Try
      `signal_workflow_sync(
          f"{WORKFLOW_ID_PREFIX}{job_id}",
          "submit_answers",
          {
              "resume_token": data["resume_token"],
              "answers": data.get("submitted_answers") or [],
          },
      )`.
   2. On success → `True`.
   3. On any exception → log error with `exc_info`, return `False`.
4. Else: existing thread-mode path unchanged (heartbeat deferral, plan
   recovery, GitHub token, `_claim_and_spawn_resume`).

Import `signal_workflow_sync` and `WORKFLOW_ID_PREFIX` into `orchestration.py`
(or use existing import paths if already present). Update the function
docstring so preconditions/postconditions document the Temporal branch and
that signal failures degrade to `False` rather than raising.

## Testing

Add coverage in `tests/test_coding_team_api_hitl.py` (near other
`_try_auto_resume` tests):

- **Signal path:** job with `resume_token`, `status=waiting_for_user`, and
  optional non-empty `submitted_answers`. `_try_auto_resume(job_id, job)`
  returns `True`; `signal_workflow_sync` was called with
  `workflow_id="coding_team-{job_id}"`, signal `"submit_answers"`, and the
  expected payload; `_claim_and_spawn_resume` (and ideally plan/token helpers)
  are not invoked.
- **Signal failure:** `signal_workflow_sync` raises → returns `False`; no
  claim/spawn.
- **AC coverage:** existing
  `test_answers_temporal_native_signals_workflow_and_appends_without_clearing`
  already asserts submitting answers signals the workflow; leave it in place
  (no change required unless a regression appears).

Existing thread-mode `_try_auto_resume` tests remain valid without
modification.

## Files

| File | Change |
|---|---|
| `backend/agents/software_engineering_team/api/orchestration.py` | Temporal early branch in `_try_auto_resume`; imports; docstring |
| `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py` | New Temporal auto-resume signal + failure tests |

## Non-goals

- Changing `/resume` or Temporal `/answers` inline signaling.
- Deleting `_claim_and_spawn_resume` / job-store claim APIs.
- Extracting a shared signal helper across routes.
- SE `/run-team` auto-resume or `RunTeamWorkflowV2`.
