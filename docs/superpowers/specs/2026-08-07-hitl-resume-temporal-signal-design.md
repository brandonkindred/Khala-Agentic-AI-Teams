# Design: HITL `/resume` signals CodingTeamWorkflow

Date: 2026-08-07

## Goal

Change `POST /run/{job_id}/resume` so that Temporal-native paused jobs
(those with a `resume_token` on the job record) wake the running
`CodingTeamWorkflow` via the `submit_answers` signal, instead of acquiring a
resume claim and spawning `_start_orchestrator_thread` /
`_start_github_resume_thread`.

Manual resume for Temporal-mode jobs becomes durable and race-free by
construction: Temporal owns signal delivery. No new claim/heartbeat logic is
added; thread-mode claim+spawn is retained until sibling cleanup issues remove
it.

## Context

Part of the coding-team HITL Temporal redesign (parent epic). Dependency
"wire workflow loop to re-invoke pipeline activity on signal" is already
landed: `CodingTeamWorkflow` waits on `submit_answers` after a paused
activity result.

`POST /run/{job_id}/answers` already mode-branches on `resume_token`: when
present it appends answers, then calls
`signal_workflow_sync(f"{WORKFLOW_ID_PREFIX}{job_id}", "submit_answers",
{resume_token, answers})`. `/resume` still always takes the claim+spawn path.

Contract reference: `system_design/hitl_pause_resume_contract.md` §3 notes that
`/resume`'s cross-worker lease is unnecessary for Temporal-mode jobs and is
retained only for thread-mode.

Out of scope for this change: the auto-resume trigger after `/answers`
(sibling), deleting claim/heartbeat machinery (later siblings), and early
`submit_answers` buffering.

## Decisions

| Topic | Choice |
|---|---|
| Mode detection | Same as `/answers`: presence of `resume_token` on the job record selects the Temporal path |
| Temporal action | `signal_workflow_sync` with signal name `submit_answers` |
| Workflow id | `f"{WORKFLOW_ID_PREFIX}{job_id}"` (`WORKFLOW_ID_PREFIX = "coding_team-"`) |
| Signal payload | `{"resume_token": <job.resume_token>, "answers": <job.submitted_answers or []>}` |
| Thread-mode path | Unchanged claim+spawn via `_claim_and_spawn_resume` |
| Shared helper | Not extracted in this change; keep an early branch inside `resume_job` (DRY with `/answers` deferred) |
| Pre-signal gates (Temporal) | Job exists; not terminal; `status == waiting_for_user` |
| Skipped on Temporal path | Thread liveness, answer-wait heartbeat, plan/repo recovery, GitHub token resolution, resume claim, spawn |
| Terminal / missing job errors | Unchanged response shape and status codes |
| Success response (Temporal) | `RunResponse(job_id=..., status="running", message="Job resumed.")` |

## Behavior

`resume_job` in `api/routes/coding_team_hitl.py`:

1. Load job → **404** `"Job not found"` if missing.
2. If `hitl.is_terminal(data)` → **400** with the existing
   `"Job is {status} and cannot be resumed."` detail.
3. If `data.get("resume_token")` is truthy (Temporal-native pause):
   1. If `data.get("status") != hitl.WAITING_STATUS` → **400** with the
      existing non-paused detail (same string as today's thread path).
   2. Call
      `signal_workflow_sync(
          f"{WORKFLOW_ID_PREFIX}{job_id}",
          "submit_answers",
          {
              "resume_token": data["resume_token"],
              "answers": data.get("submitted_answers") or [],
          },
      )`.
   3. Return `RunResponse(job_id=job_id, status="running", message="Job resumed.")`.
4. Else: existing thread-mode path unchanged (liveness / heartbeat no-op,
   waiting-status gate, plan recovery, GitHub token, `_claim_and_spawn_resume`).

`submitted_answers` may be empty; the signal still carries `answers: []`.
Duplicate delivery for an already-acknowledged token is handled by
`CodingTeamWorkflow.submit_answers` (first matching batch wins; later
matching signals ignored) — the route does not add custom idempotency.

Signal delivery failures from `signal_workflow_sync` propagate as today for
`/answers` (no new catch-and-rewrite layer in this change).

## Testing

Add route coverage in `tests/test_coding_team_api_hitl.py`:

- **Signal path:** job with `resume_token`, `status=waiting_for_user`, and
  optional non-empty `submitted_answers`. `POST /run/{id}/resume` returns
  200 `"Job resumed."`; `signal_workflow_sync` was called with
  `workflow_id="coding_team-{job_id}"`, signal `"submit_answers"`, and the
  expected payload. Assert claim/spawn helpers are not invoked.
- **Terminal unchanged:** paused-vs-terminal setup still returns the existing
  **400** body for a completed/cancelled/failed job (reuse or extend the
  current terminal resume test; must not depend on `resume_token` changing
  that outcome — terminal check runs before the Temporal branch).

Existing thread-mode `/resume` tests (no `resume_token`) remain valid without
modification.

## Files

| File | Change |
|---|---|
| `backend/agents/software_engineering_team/api/routes/coding_team_hitl.py` | Early Temporal branch in `resume_job`; docstring update for dual-mode behavior |
| `backend/agents/software_engineering_team/tests/test_coding_team_api_hitl.py` | New Temporal `/resume` signal assertion; confirm terminal case still covered |

## Non-goals

- Changing `/answers` or `_try_auto_resume` (sibling auto-resume issue).
- Deleting `_claim_and_spawn_resume` / job-store claim APIs.
- WorkflowEnvironment pause→signal→resume tests (separate coverage issue).
- SE `/run-team/{job_id}/resume` or `RunTeamWorkflowV2`.
