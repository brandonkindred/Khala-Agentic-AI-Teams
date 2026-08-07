# Design: WorkflowEnvironment test for worker restart while HITL-paused

Date: 2026-08-07

## Goal

Prove that a `CodingTeamWorkflow` pause parked on `workflow.wait_condition`
survives a Temporal worker restart, and that a single `submit_answers` signal
delivered while no worker is running is buffered by Temporal and resumes the
workflow once a new worker starts.

The basic pause → signal → resume → completion cycle is already covered by an
existing `WorkflowEnvironment` integration test in
`test_coding_team_temporal_workflow.py`. This change adds only the
worker-restart-while-paused case.

## Context

`CodingTeamWorkflow.submit_answers` deliberately drops (does not buffer) a
signal that arrives before `_active_resume_token` is set. Therefore any test
that signals while the worker is down must first confirm the workflow has
processed the paused activity result and entered `wait_condition`. Otherwise a
premature signal is dropped permanently and the test fails for the wrong
reason.

No production workflow or route changes are in scope. Early-signal buffering
and a pause-status query remain separate work.

## Decisions

| Topic | Choice |
|---|---|
| Scope | One new `@pytest.mark.integration` test; keep existing pause/resume test unchanged in behavior |
| Harness | Same `WorkflowEnvironment.start_time_skipping()` pattern as the existing coding-team / code-review Temporal integration tests |
| Activity | Fake `coding_team_run_pipeline` registered under the production activity name (paused until `acknowledged_resume_token` matches) |
| Worker lifecycle | Keep one environment alive; stop Worker A after pause is parked; start Worker B on the same task queue |
| Sync before stop | Poll `handle.fetch_history()` until an `ACTIVITY_TASK_COMPLETED` is followed by a later `WORKFLOW_TASK_COMPLETED` |
| Signal timing | Exactly one `submit_answers` while Worker A is down (server-buffered); no resend loop |
| Sticky execution | Test workers use `max_cached_workflows=0` so Worker B can pick up a task scheduled after Worker A stopped without waiting on sticky schedule-to-start timeout |
| Time skipping | Wrap pause/signal/result waits in `env.auto_time_skipping_disabled()` so unbounded `wait_condition` does not auto-advance to the workflow run timeout |
| Determinism guard | Replay completed history with `Replayer(workflows=[CodingTeamWorkflow])` |
| Production code | Unchanged |

## Behavior

Test sequence:

1. Start time-skipping `WorkflowEnvironment` and Worker A with the fake pipeline activity.
2. Start `CodingTeamWorkflow`; first activity call returns `{"outcome": "paused", "resume_token": ...}`.
3. Poll history until pause is parked (`ACTIVITY_TASK_COMPLETED` then a later `WORKFLOW_TASK_COMPLETED`). Fail with a clear diagnostic if that does not happen within ~10s.
4. Stop Worker A (environment stays up). Test workers disable sticky caching
   (`max_cached_workflows=0`) so the signal-triggered workflow task is not
   pinned to Worker A.
5. Send one `submit_answers` signal with the matching `resume_token` and a well-formed `answers` list.
6. Start Worker B on the same task queue / workflows / activities.
7. Await workflow result under `auto_time_skipping_disabled` (timeout ~30s);
   assert terminal `{"job_id": ..., "status": "completed"}`.
8. Replay recorded history; must not raise non-determinism errors.

If `wait_condition` / signal wiring regresses, or history sync is wrong and the
single signal is dropped, step 7 times out.

## Files

| File | Change |
|---|---|
| `backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py` | Refactor env/worker helper for stop/restart across one env; add the new integration test |
| `docs/superpowers/specs/2026-08-07-hitl-workflow-env-worker-restart-design.md` | This design |

## Non-goals

- Changing `CodingTeamWorkflow`, HITL routes, or claim/heartbeat cleanup.
- Rewriting the existing pause → signal → resume integration test.
- HTTP-level `/answers` or `/resume` coverage (covered elsewhere).
- GitHub-hook activity `WorkflowEnvironment` tests.
- Adding a production pause-status query or early-signal buffering.
