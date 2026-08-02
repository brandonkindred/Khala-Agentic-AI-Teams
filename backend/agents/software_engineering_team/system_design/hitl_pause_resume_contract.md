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
    "pause_context": {"task_ids": [str, ...]} | None,  # set for "worker_escalation": every task
                                                # that escalated into THIS pause round (usually one,
                                                # but see concurrent-escalation note below), so each
                                                # answer can be attached to its own task on resume
    "pending_questions": [...],       # hitl.py's structured question shape; for "worker_escalation"
                                       # each question dict also carries "task_id" (a new field,
                                       # since neither this shape nor hitl.answers_to_resolved()'s
                                       # output otherwise identifies which task asked it) so a
                                       # batched multi-worker pause can be partitioned back out
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

**Idempotency across activity retries:** Temporal can retry
`run_pipeline_activity` (worker crash, timeout, etc.) after the pause has
already been persisted to the job record but before the activity call
returned "paused" to the workflow. On (re-)entry the orchestrator must first
check the job record for an already-persisted, unresolved pause — but this
check alone is ambiguous with the *normal* resume path: after a genuine
signal-driven resume, `waiting_for_answers` is still `True` in the job
record (per the note below, only the orchestrator itself clears it, and it
hasn't yet on this very invocation), so a naive "unresolved pause exists →
re-emit it" rule would re-pause on every single resume before ever reaching
the code that applies the resolved answers — an infinite loop, not just a
retry-race fix. The two cases must be told apart explicitly: every resumed
`request` therefore carries `request["acknowledged_resume_token"]`, set by
the workflow to the `resume_token` it is resolving (see §2). On (re-)entry:
- If a persisted, unresolved pause exists **and**
  `request.get("acknowledged_resume_token") == persisted_resume_token`: this
  invocation is the one meant to consume it. Apply the resolved
  `resolved_questions` / `task_decision_overrides`, clear the pause envelope,
  and continue execution normally — do **not** re-emit.
- If a persisted, unresolved pause exists **and** the token doesn't match
  (missing, or a stale/older token): this is a genuine pre-work activity
  retry — re-emit the exact `resume_token` / `pending_questions` /
  `pause_kind` / `pause_context` unchanged rather than minting a new one, so
  a client that already saw the original token isn't rejected forever by the
  signal handler's token check.
- Otherwise (no persisted pause), proceed normally; a fresh `resume_token` is
  minted only when persisting a genuinely new pause.

This idempotency check is only correct if the job record's pause envelope
(`waiting_for_answers`, `pending_questions`, `resume_token`, `pause_kind`,
`pause_context`) is the *sole* responsibility of the orchestrator, cleared
only once it actually consumes the resolved answers on a later invocation —
**not** by the answers-submission route itself. The route's only job is to
signal the running workflow (see §2/§3); it must not also clear
`waiting_for_answers` in the job record just because a client answered early
(before the activity ever returned "paused"). Otherwise a client that answers
in that early window, followed by a worker crash and activity retry, would
have the retry see a job record with no unresolved pause (wrongly cleared)
even though the workflow never received or acknowledged it — the signal is
only buffered in workflow memory, not reflected in the job record, so the
orchestrator would either mint a spurious new pause or proceed as if nothing
was ever asked, silently dropping the human's answer.

**Postcondition:** the activity invocation is now short-lived, bounded by
actual planning/codegen work between pause points rather than by human
think-time. `start_to_close_timeout` can shrink accordingly and no longer
needs to cover hours of waiting.

**Concurrent worker-escalation serialization:** multiple implementation
workers can hit `needs_decision` in the same concurrent round. Today,
`_pause_lock` serializes this only because the entire pause blocks
synchronously while the lock is held; once the activity returns at the first
`"paused"` result, that lock is released as the call stack unwinds, so a
second, still-running worker could otherwise overwrite the job record's
`pending_questions` / `resume_token` before the first pause is even resolved.
The orchestrator must instead drain: once one worker escalates, no further
tasks are newly assigned, and workers already mid-flight get a bounded window
to finish or also escalate before the activity returns. Every worker that
escalates within that window is folded into the *same* pause
(`pause_context.task_ids` lists all of them; `pending_questions` is their
concatenated batch) rather than each publishing a competing one. Leaving a
timed-out worker's task merely marked `in_progress` is not sufficient: it
does not stop that worker's thread, which can keep mutating the task graph,
job record, and repo worktree after the activity has already returned, while
the *next* invocation's `reset_in_flight()` starts a second, concurrent
attempt at the same task. Workers that exceed the drain window must instead
be cooperatively cancelled (a cancellation signal each worker's loop checks
between steps) and the activity must wait for actual quiescence — every
worker either completed, escalated into the pause, or observably stopped —
before returning `"paused"`. Only a worker that has genuinely stopped may be
left for `reset_in_flight()` to restart on resume.

`reset_in_flight()` (`task_graph.py:174-195`) demotes both `IN_PROGRESS`
*and* `IN_REVIEW` tasks to unassigned `TO_DO` — an explicit postcondition
("no task is `IN_PROGRESS` or `IN_REVIEW`"), not an oversight to work around
quietly. If a second worker legitimately *finishes* implementation and
reaches `IN_REVIEW` during the same drain window as another worker's
escalation, quiescence alone isn't enough: the resumed activity's
`reset_in_flight()` call would demote that finished task back to `TO_DO`
too, discarding completed work and potentially churning or overwriting its
feature branch on a redone attempt. The drain must therefore push any task
that reaches `IN_REVIEW` during the window through its normal review/merge
gate before the activity returns `"paused"` — quiescence means every worker
is either still cleanly `TO_DO`-restartable, cooperatively stopped, or past
`IN_REVIEW` entirely, never left sitting at `IN_REVIEW` for `reset_in_flight()`
to sweep away.

### 2. Workflow contract

The workflow (`CodingTeamWorkflow`, and SE's `RunTeamWorkflowV2`) gains:

- `@workflow.signal submit_answers(payload: dict)` — a single payload
  `{"resume_token": str, "answers": list}`, matching this codebase's existing
  signal convention of one payload argument per signal (e.g.
  `signal_workflow_sync`'s `handle.signal(signal_name, *args)` and
  `agentic_team_provisioning`'s `submit_input` signal, both called with at
  most one value; the installed Temporal Python SDK's `WorkflowHandle.signal`
  itself only takes one positional `arg`, with `args=[...]` needed for more
  than one, so a single dict payload avoids that entirely and stays
  consistent with how every other signal in this repo is already called). If
  `payload["resume_token"] != self._active_resume_token`, the signal is a
  stale or duplicate submission (e.g. a retried HTTP call, or an answer to a
  pause that already resolved) and must be **ignored**, not applied.
  Otherwise sets `self._submitted_answers = payload["answers"]`. This is the
  *only* state the wait condition gates on; the handler must not depend on
  the workflow having already observed the "paused" outcome, since the
  activity persists `waiting_for_answers=True` to the job record (and a
  client can act on that) before the activity call itself returns — so an
  early signal can arrive before `self._active_resume_token` is even set. In
  that case the handler buffers `payload`; once the workflow observes
  "paused" and learns the token, it checks the buffer for a match before
  waiting.
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

    # hitl.answers_to_resolved(submitted_answers, pending_questions) needs BOTH
    # args -- it recovers question_text/answer labels from pending_questions;
    # a submitted answer only carries question_id + a selected option. Passing
    # just the answers can't produce a real resolved record, and
    # hitl.unanswered_questions() (text-matched) would then treat the decision
    # as still-unanswered and re-ask it next round.
    resolved = hitl.answers_to_resolved(self._submitted_answers, self._pending_questions)
    if result["pause_kind"] == "worker_escalation":
        # Must land on each paused task's own revision_feedback, not the
        # plan-level list -- run_implement reads a task's own
        # revision_feedback, not plan_input.resolved_questions, so a
        # plan-level-only merge lets the worker re-ask the same question.
        # resolved records carry no task_id of their own (see
        # hitl.answers_to_resolved's fields) -- partition them by mapping
        # question_id -> task_id from self._pending_questions instead, using
        # the "task_id" field added to each worker-escalation question above.
        question_task_id = {q["id"]: q["task_id"] for q in self._pending_questions}
        by_task: Dict[str, list] = {}
        for r in resolved:
            by_task.setdefault(question_task_id[r["question_id"]], []).append(r)
        # task_decision_overrides carries the SAME feedback envelope shape
        # _escalate_decision already builds (pause_cycle.py / swarm_implementation.py)
        # -- {"source": "user_decision", "reason": _format_decisions(...), 
        # "requested_changes": [], "decisions": ...} -- not the raw resolved
        # records: v2_team_worker._feedback_lines() only renders "reason" /
        # "error" / "message" / "requested_changes", none of which exist on a
        # bare resolved record, so the worker would never actually see the
        # answer text and could repeat the same decision. Reusing this exact
        # shape also keeps the existing per-task escalation-cap accounting
        # (which counts entries with source == "user_decision") intact.
        # Replaced, not merged, on every round: an override is consumed by the
        # orchestrator on the very next invocation, so carrying a prior
        # round's entry forward would reapply it and duplicate history.
        request["plan_input"]["task_decision_overrides"] = {
            task_id: {
                "source": "user_decision",
                "reason": pause_cycle._format_decisions(task_resolved),
                "requested_changes": [],
                "decisions": task_resolved,
                "resume_token": self._active_resume_token,  # idempotency key, see §3
            }
            for task_id, task_resolved in by_task.items()
        }
    else:  # "entry" | "tech_lead_clarify"
        request["plan_input"]["resolved_questions"] = (
            request["plan_input"].get("resolved_questions", []) + resolved
        )
    request["job_id"] = result["job_id"]
    # Tells the orchestrator THIS invocation is the one resolving the
    # persisted pause it will still find in the job record (only the
    # orchestrator clears waiting_for_answers, and only after consuming this
    # acknowledgment) -- without it, re-entry can't tell a genuine resume
    # apart from a pre-work activity retry and would re-pause forever. See
    # the idempotency note in §1.
    request["acknowledged_resume_token"] = result["resume_token"]

    self._submitted_answers = None
    self._pending_questions = None
    self._active_resume_token = None
```

### 3. Resume contract

- The client must echo back the token it was given, not have the server
  re-derive "the current" token at submission time. `resume_token` is
  returned to the client as part of the pause notification (the job
  record's `pending_questions` / status payload) when the pause first
  becomes visible. Otherwise a delayed or retried submission for pause A
  that arrives after pause B has already started would get tagged with B's
  (now-current) token by the server and pass the workflow's token check
  despite answering the wrong pause.
  - This requires an actual response-schema change, not just an assumption:
    today's `StatusResponse` (`api/coding_team_models.py:44`) and
    `JobStatusResponse` (`api/models.py:170`) expose only
    `pending_questions` / `waiting_for_answers`, and `PendingQuestion`
    (`shared/hitl/models.py:43`) has no token field. `resume_token` is
    per pause *round* (shared by every question in a batch), not
    per-question, so it belongs as a new top-level
    `resume_token: Optional[str] = None` field on both status response
    models — populated from the job record's persisted `resume_token` — not
    added to `PendingQuestion`. Without this, a client that discovers the
    pause by polling status (rather than only from the original pause
    notification) has no way to obtain the token it's required to echo.
  - `SubmitAnswersRequest` (`api/coding_team_state.py`) is a **shared**
    model already consumed by three routes: coding-team's
    `/run/{job_id}/answers`, SE's `/run-team/{job_id}/answers`, and the
    unrelated `product_analysis.submit_product_analysis_answers` — none of
    which have any concept of a resume token today, and none of which are
    always native-Temporal-signal-capable (see the mode branch below). The
    field must therefore be added as `resume_token: Optional[str] = None`,
    not required — a required field would 422 every one of today's
    callers. Each route enforces its presence explicitly (a plain check,
    not Pydantic validation) only in the native-Temporal-signal branch.
- **Both coding-team and SE answers routes need a three-way mode branch**,
  not a binary Temporal/thread split — a Temporal-mode job is not
  automatically signal-capable:
  - **Native-signal-capable Temporal workflows** (`CodingTeamWorkflow`, and
    `RunTeamWorkflowV2` when `SE_WORKFLOW_V2` selects it): signal without
    touching the job record's pause envelope (per the idempotency note in
    §1 — clearing it is the orchestrator's job alone, once it actually
    consumes the answer).
    - `POST /run/{job_id}/answers` (coding-team-only jobs,
      `api/routes/coding_team_hitl.py`) signals the `CodingTeamWorkflow` run,
      whose workflow ID is `f"{WORKFLOW_ID_PREFIX}{job_id}"` using the
      existing `WORKFLOW_ID_PREFIX = "coding_team-"` constant
      (`temporal/coding_team_constants.py`).
    - `POST /run-team/{job_id}/answers` (SE-level jobs, `api/routes/hitl.py`)
      currently only writes to the job store; it must equally be updated to
      signal the corresponding `RunTeamWorkflowV2` run, whose workflow ID is
      `f"{WORKFLOW_ID_PREFIX_RUN_TEAM}{job_id}"` using the existing
      `WORKFLOW_ID_PREFIX_RUN_TEAM = "se-run-team-"` constant
      (`temporal/constants.py`) — not an invented prefix. Missing this means
      SE Temporal jobs would have their job-store `waiting_for_answers` flag
      cleared but their workflow never learns of it, and it waits
      indefinitely.
    - Both `await handle.signal("submit_answers", {"resume_token":
      client_resume_token, "answers": answers})` — a single dict payload,
      using the client-echoed token described above and matching this
      repo's single-payload signal convention.
  - **Everything else keeps today's store-and-clear behavior unchanged** —
    write `submitted_answers` and clear `waiting_for_answers` in the job
    record directly, since there is no signal handler to reach:
    - **Thread-mode jobs** (e.g. the still-threaded `run-from-github` flow),
      where `hitl.wait_for_answers`'s poll loop is exactly what the
      store-and-clear write unblocks.
    - **SE V1 Temporal jobs** — when `SE_WORKFLOW_V2` is unset/false,
      `/run-team` launches `RunTeamWorkflow` (`temporal/workflows.py:40`),
      whose activity still blocks in the SE `orchestrator.py`'s own
      `_wait_for_user_answers` poll loop and whose workflow defines no
      `submit_answers` signal at all. Signaling a V1 job would target a
      workflow with no such handler; it must keep writing to the job store
      like today until V1 is migrated or removed.
    The route must check both the job's mode (thread vs. Temporal) *and*,
    for Temporal jobs, whether the actual running workflow is
    signal-capable (V2) before choosing a branch — attempting a signal
    against a workflow ID that doesn't define `submit_answers`, or that
    isn't the one actually running, silently fails to unblock it.
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
    `task_decision_overrides: Dict[str, dict]` (task ID → a single feedback
    entry already shaped like `_escalate_decision`'s existing `user_decision`
    envelope: `{"source": "user_decision", "reason": ..., "requested_changes":
    [], "decisions": [...], "resume_token": ...}` — not a bare list of
    resolved records, which `v2_team_worker._feedback_lines()` cannot render
    (it reads `reason`/`error`/`message`/`requested_changes`, none of which
    exist on a raw `hitl.answers_to_resolved()` record) and which would also
    break the existing per-task escalation-cap accounting that counts
    `source == "user_decision"` entries. The envelope's `resume_token` field
    (new — not read by `_feedback_lines()`, purely an idempotency key) is
    what makes application itself retry-safe: `TaskGraphService.snapshot()`
    serializes `revision_feedback`, and `GraphPersistCoordinator` persists
    that snapshot after every graph mutation — so if the activity appends
    this envelope and then fails before returning, a Temporal retry would
    restore a snapshot that *already* contains it, and re-appending on top
    would duplicate the entry. Immediately after `graph.restore()` on
    re-entry, the orchestrator checks the restored task's `revision_feedback`
    for an existing entry with this same `resume_token` before appending —
    skip if already present, append otherwise. Before handing the task to
    `run_implement` again, the orchestrator must also re-run
    `_escalate_decision`'s own cap check (`swarm_implementation.py:472-491`):
    count existing `source == "user_decision"` entries in the (now-updated)
    `revision_feedback` against `MAX_TASK_REVISIONS`, and if the cap is
    exceeded, mark the task `FAILED` and cascade-fail its dependents instead
    of re-queuing it — that check lives inside `_escalate_decision`'s
    post-pause continuation today, which this pause/resume redesign bypasses
    entirely (the activity returns immediately rather than blocking then
    continuing inline), so it must be re-implemented at the point overrides
    are applied, not assumed to still run automatically. The orchestrator
    must not re-emit the same
    `task_decision_overrides` entry on a later invocation for a *different*
    pause round either (the workflow already replaces rather than
    accumulates it per round, per §2, but the orchestrator side must not
    persist it back into the snapshot as a pending override), or a later
    resume could apply the same decision to the task twice under a
    different guise.
  - **This field alone does not reach `RunTeamWorkflowV2`.** Its activity,
    `execute_coding_team_activity(job_id, repo_path, plan_result,
    resolved_questions_override=None, trace_id="")`
    (`temporal/activities.py:644`), never receives or forwards a full
    `plan_input` dict — it rebuilds `CodingTeamPlanInput` itself via
    `_build_coding_team_plan_input(adapter_result, path, existing_code,
    resolved_questions_override)`, using its own individual keyword
    argument for entry/Tech-Lead answers. `execute_coding_team_activity`
    must gain its own new `task_decision_overrides` keyword parameter,
    mirrored alongside `resolved_questions_override` and threaded through to
    `_build_coding_team_plan_input`, and `RunTeamWorkflowV2`'s resume loop
    must pass it explicitly on each activity call — adding the model field
    fixes only the standalone `CodingTeamWorkflow` path. `acknowledged_resume_token`
    (§1) needs the same explicit threading: `execute_coding_team_activity`
    gains an `acknowledged_resume_token: Optional[str] = None` parameter
    alongside the others, since it has no `request` dict for the workflow to
    set a bare key on.

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
