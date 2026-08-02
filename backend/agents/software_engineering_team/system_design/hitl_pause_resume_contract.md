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
    "resume_token": str,              # unique PER PAUSE ROUND (e.g. f"{job_id}:{pause_seq}"),
                                       # NOT job_id -- correlates a submitted answer batch with
                                       # the specific pause it answers (see wait_condition race below)
    "pause_kind": str,                # "entry" | "tech_lead_clarify" | "worker_escalation"
    "pause_context": {"task_id": str} | None,  # set for "worker_escalation": identifies which
                                                # task raised the question, so its answer can be
                                                # attached to that task specifically on resume
    "pending_questions": [...],       # same structured shape hitl.py already produces
    "task_graph_snapshot": {...},     # already persisted; included for the caller's convenience
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

- `@workflow.signal submit_answers(resume_token: str, answers: list)` — if
  `resume_token != self._active_resume_token`, the signal is a stale or
  duplicate submission (e.g. a retried HTTP call, or an answer to a pause
  that already resolved) and must be **ignored**, not applied. Otherwise sets
  `self._submitted_answers = answers`. This is the *only* state the wait
  condition gates on; the handler must not depend on the workflow having
  already observed the "paused" outcome, since the activity persists
  `waiting_for_answers=True` to the job record (and a client can act on
  that) before the activity call itself returns — so an early signal can
  arrive before `self._active_resume_token` is even set. In that case the
  handler buffers the `(resume_token, answers)` pair; once the workflow
  observes "paused" and learns the token, it checks the buffer for a match
  before waiting.
- `@workflow.query pending_questions() -> list` / `status() -> dict` — for
  polling clients, mirroring `CodeReviewWorkflow.progress()`. `status()`
  derives `waiting_for_answers` as
  `self._pending_questions is not None and self._submitted_answers is None`
  rather than tracking a separately-mutated boolean.

Its main loop becomes:

```python
while True:
    result = await workflow.execute_activity(
        run_pipeline_activity, request, start_to_close_timeout=<short>,
    )
    if result["outcome"] in ("completed", "failed"):
        return result
    # outcome == "paused". self._submitted_answers may already be populated
    # here if a client signaled before this call returned - do not
    # unconditionally rearm a separate "waiting" flag, or that early signal
    # is silently lost and wait_condition never resolves. Gate on the answers
    # themselves, not on a flag the pause and the signal both mutate.
    self._active_resume_token = result["resume_token"]
    self._pending_questions = result["pending_questions"]
    self._check_buffered_signal_for(self._active_resume_token)  # apply a signal that beat us here
    await workflow.wait_condition(lambda: self._submitted_answers is not None)

    # Convert to resolved-question records (same shape hitl.answers_to_resolved
    # produces) and merge them into fields the activity's RunRequest/
    # CodingTeamPlanInput actually define -- not a bare extra key, which
    # run_pipeline_activity's RunRequest parsing would silently discard.
    resolved = to_resolved_records(self._submitted_answers)
    if result["pause_kind"] == "worker_escalation":
        # Must land on the specific paused task's revision_feedback, not the
        # plan-level list -- run_implement reads a task's own
        # revision_feedback, not plan_input.resolved_questions, so a
        # plan-level-only merge lets the worker re-ask the same question.
        # task_decision_overrides is a new CodingTeamPlanInput field (see
        # resume contract below); the orchestrator applies it to the
        # restored task right after graph.restore().
        task_id = result["pause_context"]["task_id"]
        request["plan_input"]["task_decision_overrides"] = {
            **request["plan_input"].get("task_decision_overrides", {}),
            task_id: resolved,
        }
    else:  # "entry" | "tech_lead_clarify"
        request["plan_input"]["resolved_questions"] = (
            request["plan_input"].get("resolved_questions", []) + resolved
        )
    request["job_id"] = result["job_id"]

    self._submitted_answers = None
    self._pending_questions = None
    self._active_resume_token = None
```

### 3. Resume contract

- The client must echo back the token it was given, not have the server
  re-derive "the current" token at submission time. `resume_token` is
  returned to the client as part of the pause notification (the job
  record's `pending_questions` / status payload) when the pause first
  becomes visible. `SubmitAnswersRequest` gains a required `resume_token`
  field; the answers route uses *that* value, not whatever `resume_token`
  happens to be current in the job record at submission time. Otherwise a
  delayed or retried submission for pause A that arrives after pause B has
  already started would get tagged with B's (now-current) token by the
  server and pass the workflow's token check despite answering the wrong
  pause.
- **Both** answers-submission surfaces must dispatch a signal to their
  matching workflow type, not just the coding-team one:
  - `POST /run/{job_id}/answers` (coding-team-only jobs,
    `api/routes/coding_team_hitl.py`) signals the `CodingTeamWorkflow` run,
    whose workflow ID is `f"{WORKFLOW_ID_PREFIX}{job_id}"` using the existing
    `WORKFLOW_ID_PREFIX = "coding_team-"` constant
    (`temporal/coding_team_constants.py`).
  - `POST /run-team/{job_id}/answers` (SE-level jobs,
    `api/routes/hitl.py`) currently only writes to the job store; it must
    equally be updated to signal the corresponding `RunTeamWorkflowV2` run,
    whose workflow ID is `f"{WORKFLOW_ID_PREFIX_RUN_TEAM}{job_id}"` using the
    existing `WORKFLOW_ID_PREFIX_RUN_TEAM = "se-run-team-"` constant
    (`temporal/constants.py`) — not an invented prefix. Missing this means SE
    Temporal jobs would have their job-store `waiting_for_answers` flag
    cleared but their workflow never learns of it, and it waits indefinitely.
  - Both routes call `handle.signal("submit_answers", client_resume_token,
    answers)` using the client-echoed token described above.
- `POST /run/{job_id}/resume`'s cross-worker lease mechanism
  (`resume_claim_at` / `resume_claim_seq` in `job_store.py`) becomes
  unnecessary for Temporal-mode jobs — Temporal itself durably tracks a
  waiting workflow across worker restarts — and is retained only for
  thread-mode jobs.
- Orchestrator re-entry still loads `task_graph_snapshot` via
  `graph.restore()` + `reset_in_flight()` exactly as today. Neither
  `RunRequest` (fields: `repo_path`, `plan_input`) nor `CodingTeamPlanInput`
  currently has anywhere to carry a per-task decision, so the entry/Tech-Lead
  case and the worker-escalation case need two different, explicitly-defined
  landing spots rather than one invented helper call:
  - **Entry/Tech-Lead pauses:** the resolved-question records are merged
    into `plan_input.resolved_questions` (the field already exists on
    `CodingTeamPlanInput` and the orchestrator already reads it) before the
    next `run_pipeline_activity` call.
  - **Worker-escalation pauses:** `CodingTeamPlanInput` gains a new field,
    `task_decision_overrides: Dict[str, List[dict]]` (task ID → resolved
    answer records). Immediately after `graph.restore()` on re-entry, the
    orchestrator applies each override to the matching restored task's
    `revision_feedback` *before* that task is handed to `run_implement`
    again — this is a new, explicit contract field, not a bare pass-through
    of an ad hoc object the activity's existing parsing would silently
    ignore.

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
