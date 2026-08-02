# HITL Pause/Resume Contract (Temporal Signal + wait_condition Redesign)

This is the pause/resume contract required before any other sub-issue of the
native-Temporal HITL redesign begins. It covers what the coding-team Temporal
activity must return when the pipeline pauses for human input, and what the
workflow must do to resume correctly. It documents the current mechanism,
then specifies the target contract and the open decisions any implementing
sub-issue must resolve.

## Current mechanism (as-is)

Today's pause is invisible to Temporal. `hitl.wait_for_answers`
(`hitl.py:499-568`) is a plain `while now() - start < timeout: sleep(5)` poll
loop against the external, job-service-backed job record's
`waiting_for_answers` flag. It is called by
`pause_cycle._wait_and_collect_answers` (`pause_cycle.py:179-247`), which
every HITL gate in `coding_team_orchestrator.py` funnels through via
`_run_pause_cycle`: the entry gate (unanswered questions on `plan_input`),
the Tech Lead clarify/re-plan loop (capped at `MAX_TECH_LEAD_QUESTION_ROUNDS
= 5`), and per-worker escalation during execution.

`run_pipeline_activity` (`temporal/coding_team_workflow.py`) calls all the way
down into this blocking loop and does not return until the job reaches a
terminal state. Temporal therefore sees one very long-running activity: no
`activity.heartbeat()` calls exist on this path, and its
`start_to_close_timeout` is a hard 4 hours, uncoordinated with `hitl.py`'s own
(separately configurable, default 1-hour) answer-wait timeout. The SE-level
analogue, `execute_coding_team_activity` (`temporal/activities.py:643-753`),
wraps the same blocking call in a `BackgroundHeartbeat` so the activity itself
doesn't time out, but this only extends how long the activity can block — it
does not make the pause Temporal-native.

All pause state lives outside Temporal, in the job-service-backed record
(`job_store.py`): `waiting_for_answers`, `pending_questions`,
`submitted_answers`, plus `task_graph_snapshot` / `agent_task_map` for resume
(persisted continuously by `GraphPersistCoordinator`, independent of whether a
pause is active). A working precedent for the target pattern already exists
in this codebase: `code_review_agent/temporal/workflows.py`'s
`CodeReviewWorkflow` uses `@workflow.signal cancel()` +
`@workflow.query progress()` to let an external caller poke and read a live
workflow. No equivalent exists yet for HITL answers.

## Target contract

### 1. Activity return contract

`run_pipeline_activity` (and the SE-level analogue
`execute_coding_team_activity`) must stop blocking through a pause. When a
HITL gate would otherwise call `hitl.wait_for_answers`, the orchestrator
instead returns immediately from the activity with a discriminated result:

```python
# Paused, waiting on a human:
{
    "outcome": "paused",
    "job_id": str,
    "pending_questions": [...],       # same structured shape hitl.py already produces
    "task_graph_snapshot": {...},     # already persisted; included for the caller's convenience
    "resume_token": str,              # opaque; == job_id today, reserved for future use
}

# Finished normally:
{"outcome": "completed", "job_id": str, ...final job record fields...}

# Unrecoverable failure:
{"outcome": "failed", "job_id": str, "error": str, ...}
```

**Precondition:** the orchestrator has already durably persisted
`waiting_for_answers=True` and `pending_questions` to the job record (as it
does today) before returning `"paused"` — the activity's return value is a
notification to the workflow, not the source of truth for pause state.

**Postcondition:** the activity invocation is now short-lived, bounded by
actual planning/codegen work between pause points rather than by human
think-time. `start_to_close_timeout` can shrink accordingly and no longer
needs to cover hours of waiting.

### 2. Workflow contract

The workflow (`CodingTeamWorkflow`, and SE's `RunTeamWorkflowV2`) gains:

- `@workflow.signal submit_answers(answers: list)` — records answers into
  workflow-local state and clears the local waiting flag.
- `@workflow.query pending_questions() -> list` / `status() -> dict` — for
  polling clients, mirroring `CodeReviewWorkflow.progress()`.

Its main loop becomes:

```python
while True:
    result = await workflow.execute_activity(
        run_pipeline_activity, request, start_to_close_timeout=<short>,
    )
    if result["outcome"] in ("completed", "failed"):
        return result
    # outcome == "paused"
    self._waiting_for_answers = True
    self._pending_questions = result["pending_questions"]
    await workflow.wait_condition(lambda: not self._waiting_for_answers)
    request = {**request, "job_id": result["job_id"],
               "resolved_answers": self._submitted_answers}
    self._submitted_answers = []
```

### 3. Resume contract

- `POST /run/{job_id}/answers` becomes a thin signal dispatch
  (`handle.signal("submit_answers", answers)`) instead of a raw job-record
  write.
- `POST /run/{job_id}/resume`'s cross-worker lease mechanism
  (`resume_claim_at` / `resume_claim_seq` in `job_store.py`) becomes
  unnecessary for Temporal-mode jobs — Temporal itself durably tracks a
  waiting workflow across worker restarts — and is retained only for
  thread-mode jobs.
- Orchestrator re-entry still loads `task_graph_snapshot` via
  `graph.restore()` + `reset_in_flight()` exactly as today, folding the
  signaled answers directly into `plan_input.resolved_questions` rather than
  re-reading `submitted_answers` back from the job record.

## Open questions (flagged, not resolved by this spike)

1. **Source-of-truth ownership.** Does the job-store record stay authoritative
   for pause state (workflow signal/query handlers just proxy reads/writes to
   it), or does pause state move into the workflow's own durable state, with
   the job-store record becoming an async mirror kept only for the existing
   REST/audit surface?
2. **`wait_condition` timeout.** Does it get a timeout mirroring today's
   fail-closed `hitl.answer_wait_timeout_s()` (timeout → job fails), or does
   it wait indefinitely and rely on some other mechanism (e.g. a workflow
   timer signal) for staleness handling?
3. **Round-trip granularity.** One signal round-trip per pause point (matches
   today's three independent `_run_pause_cycle` call sites), or batch
   multiple pending questions into a single richer payload? Recommendation:
   one round-trip per pause point — the entry gate, Tech Lead clarify loop,
   and per-worker escalation are already independent call sites with
   different resume semantics, and worker-level escalation in particular is
   concurrent with other in-flight work.
4. **GitHub-hook flow.** Should posting the pause as a GitHub issue comment
   (`run-from-github`'s `on_pause` callback, `hitl._format_questions_comment`)
   move into an activity invoked right after the workflow observes
   `"paused"` (so it's retryable/durable like other Temporal activities), or
   stay driven inline from the orchestrator as it is today?
5. **Duplicate poll loops.** `hitl.py`, SE `orchestrator.py`'s
   `_wait_for_user_answers`, and PRA's `user_communication.wait_for_answers`
   are three independently-implemented, near-duplicate poll loops. Consolidating
   them is out of scope for the coding-team-focused redesign under #3968 and
   is called out here only so it isn't lost.

## Sources read in full for this contract

- `backend/agents/software_engineering_team/orchestrator.py`
- `backend/agents/software_engineering_team/coding_team_orchestrator.py`
- `backend/agents/software_engineering_team/hitl.py`
- `backend/agents/software_engineering_team/pause_cycle.py`
- `backend/agents/software_engineering_team/job_store.py`
- `backend/agents/software_engineering_team/graph_persist.py`
- `backend/agents/software_engineering_team/temporal/coding_team_workflow.py`
- `backend/agents/software_engineering_team/temporal/workflows.py`
- `backend/agents/software_engineering_team/temporal/activities.py`
- `backend/agents/software_engineering_team/code_review_agent/temporal/workflows.py`
- `backend/agents/software_engineering_team/system_design/architecture.md`
