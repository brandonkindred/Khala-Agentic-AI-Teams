# HITL Pause/Resume Contract (Temporal Signal + wait_condition Redesign)

This is the pause/resume contract required before any other sub-issue of the
native-Temporal HITL redesign begins. It covers what the coding-team Temporal
activity must return when the pipeline pauses for human input, and what the
workflow must do to resume correctly. It documents the current mechanism,
then specifies the target contract and the open decisions any implementing
sub-issue must resolve.

> **Citation freshness:** file/line citations below (`module.py:NN-MM`) are
> accurate as of the commit this document was written against and will drift
> as the codebase changes. The symbol names (function/class names) they
> accompany are the authoritative reference — resolve a citation that no
> longer matches by re-locating the named symbol, not by trusting the line
> numbers.

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

`run_pipeline_activity` (`temporal/coding_team_workflow.py:12`) calls all the
way down into this blocking loop and does not return until the job reaches a
terminal state. Its home file is deliberately named `coding_team_workflow.py`,
not `activities.py`, despite the repo's general convention (activities in
`activities.py`, workflows in `workflows.py`, as `code_review_agent`'s split
follows) — the module docstring states it wraps "workflow + activity" in one
file, co-locating this specific activity with its paired workflow rather than
the SE-level `activities.py`/`workflows.py` split. This is not a typo; do not
"correct" this citation to `temporal/activities.py`. Temporal therefore sees
one very long-running activity: no `activity.heartbeat()` calls exist on this
path, and its
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
instead returns immediately from the activity with a discriminated result.

**This is a per-caller strategy, not a universal change to
`run_coding_team_orchestrator` itself.** That function is shared: both
`run_orchestrator_wired` (`api/orchestration.py:57-80`, used by the Temporal
path) and `_run_with_github_hooks` (`api/orchestration.py:877-946`, the
still-threaded `run-from-github` flow) call it directly, and the latter
passes `on_pause=_on_pause` expecting the call to stay blocked through a
pause exactly as today — there is no activity boundary in the thread-mode
path for a "return early" result to unwind through, and unconditionally
changing the shared function's behavior would make the GitHub-hook thread
return prematurely at its first pause instead of blocking until answered.
`run_coding_team_orchestrator` (and `_run_pause_cycle` beneath it) must
therefore take an explicit pause-strategy argument: Temporal-activity
callers (`run_pipeline_activity`, `execute_coding_team_activity`) request
"return on pause" and get the discriminated result below; thread-mode
callers, including `run_orchestrator_wired`'s own thread-mode uses and
`_run_with_github_hooks`, keep requesting today's "block through pause"
strategy unchanged, `on_pause` included.

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
}

# Finished normally:
{"outcome": "completed", "job_id": str, "status": str, "summary": str | None}

# Unrecoverable failure:
{"outcome": "failed", "job_id": str, "error": str}
```

**Precondition:** the orchestrator has already durably persisted
`waiting_for_answers=True` and `pending_questions` to the job record (as it
does today) before returning `"paused"` — the activity's return value is a
notification to the workflow, not the source of truth for pause state.

`task_graph_snapshot` is deliberately **not** part of this result, despite
being "already persisted" and seemingly free to include: the workflow loop
in §2 never reads it (it only consumes `resume_token` / `pending_questions` /
`pause_kind` / `pause_context`), and on resume the orchestrator reloads it
independently via `graph.restore()` (§3) rather than expecting it back from
the workflow — so it has no consumer on either side of this boundary. For a
job with a large task graph or substantial `revision_feedback`, including it
anyway risks exceeding Temporal's configured activity-result payload limit;
that would fail the *completion* of the very activity call that is supposed
to report the pause, and a retry would hit the same oversized payload again,
so the workflow could never observe `"paused"` at all. Return only the pause
metadata the workflow actually needs.

The same bound applies to `"completed"`, and more severely: §2's main loop
returns this result directly as the workflow's own return value
(`if result["outcome"] in ("completed", "failed"): return result`), so an
unbounded "final job record fields" payload risks the same activity-result
size failure the paused case was just narrowed to avoid — except here the
work (planning, codegen, merges) is already done and the repo already
mutated by the time completion is attempted, so a retry-on-oversized-payload
doesn't just fail to report progress, it re-runs completion logic against
state the first attempt already changed. The job record — the authoritative
snapshot — already holds everything a caller needs; `"completed"` and
`"failed"` should carry only a small, fixed-shape summary (job id, terminal
status, an optional short message), never the full record.

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
the workflow to the `resume_token` it is resolving (see §2). This field's
lifecycle is: set once per resolved pause round, sent on exactly the next
activity call, and cleared from `request` immediately once that call returns
(§2 shows this explicitly) — it is never persisted anywhere outside that one
in-flight `request` dict, and the orchestrator must match it by **exact
equality** against the *current* persisted pause's `resume_token`, never by
mere presence. Since `resume_token` values are unique per pause round and
never reused, a stale or absent value is automatically inert against this
check on its own; the explicit clear is defense-in-depth against a future
implementation that checks presence instead of equality, not a requirement
for correctness of the equality check itself. On (re-)entry:
- If a persisted, unresolved pause exists **and**
  `request.get("acknowledged_resume_token") == persisted_resume_token`: this
  invocation is the one meant to consume it. Apply the resolved
  `resolved_questions` / `task_decision_overrides`, **append the accepted
  answer batch to the job record's `submitted_answers` and clear the pause
  envelope in one atomic update**, and continue execution normally — do
  **not** re-emit. Two details matter here, not just the fact of
  persisting:
  - **Append, not overwrite.** `submitted_answers` is an accumulated
    history across every pause round in the job's lifetime, not a
    single-round scratch value: `job_service_client.submit_answers()`
    (`job_service_client.py:767-778`) already writes it via
    `append_to={"submitted_answers": answers}`, and `hitl.answers_to_resolved()`
    /`unanswered_questions()` both expect to see every prior round's answers
    when checking coverage. A plain assignment here would silently discard
    every earlier pause round's answers the moment a job with more than one
    pause round resolves its second one.
  - **Atomic with the envelope clear, not two writes.** If the append
    succeeded but the pause-envelope clear then failed (or the activity
    crashed between them), a retry would still find a persisted, unresolved
    pause **and** the same matching `acknowledged_resume_token` — the exact
    condition this bullet is gated on — and would append the identical
    batch a second time. The append and the clear must be one atomic job
    record write, equivalent to the store-and-clear helper's own atomicity,
    not a persist step followed by a separate clear step.

  The persist step matters beyond in-memory application:
  `system_design/architecture.md`'s "Persistence And Resume" section already
  documents "submitted HITL answers" as one of the fields the orchestrator
  persists, and status/audit consumers that read the job record depend on
  it being there. The native-signal answers route (§3) deliberately never
  writes to the job record — only the orchestrator, on this consuming
  invocation, does — so without this explicit step a native-Temporal pause's
  human answers would exist only transiently in workflow memory and vanish
  from the job record entirely once the workflow folds them into
  `resolved_questions` / `task_decision_overrides` and moves on, unlike the
  thread-mode store-and-clear path where the route itself writes
  `submitted_answers` directly.
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

Shrinking it is not free, though: unlike the worker-escalation drain (which
gains explicit cooperative cancellation and a quiescence wait above),
`run_pipeline_activity`'s general planning/codegen/build work has no such
path today. The "Current mechanism" section above establishes that the
*whole* `run_pipeline_activity` call — pause-wait included — makes zero
`activity.heartbeat()` calls (confirmed by grepping for `activity.heartbeat`
across `coding_team_orchestrator.py`, `swarm_implementation.py`, and
`temporal/coding_team_workflow.py`: no matches), so the planning/codegen/build
segments necessarily have none either, since they're a subset of that same
call — not a separately-documented fact, just a direct consequence of it. If
a single work segment between pause
points takes longer than `start_to_close_timeout`, Temporal schedules a
retry, but nothing stops the original attempt's Python execution on the
worker; it keeps running, unaware it timed out, and can now mutate the same
job record, task graph, and repo worktree concurrently with the new retry
attempt — silent corruption, not a clean restart. `start_to_close_timeout`
must therefore be set with margin above the longest plausible non-pause work
segment, not shrunk to "just enough for typical work"; if a shorter timeout
is wanted for faster failure detection, the activity needs the same
heartbeat-plus-cooperative-cancellation treatment given to the drain case, so
a timed-out attempt is confirmed stopped before its retry is allowed to run.

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
concatenated batch) rather than each publishing a competing one.

Critically, this changes *when* the job record is written relative to
today's single-worker `_run_pause_cycle` (`pause_cycle.py:283-289`), which
publishes `waiting_for_answers=True` / `pending_questions` as its first
action, before anything blocks. For a worker-escalation pause the orchestrator
must **not** carry that publish-immediately behavior forward: it must hold
the job record write until the drain reaches quiescence, then persist the
full, already-concatenated (namespaced, see below) batch in one update. If
the first worker's questions were instead published as soon as that worker
escalated, a client could poll, see that partial batch, and submit a
complete-looking answer set while the drain window is still open; a second
worker escalating moments later would be folded into `pending_questions` for
a pause the client has already answered and the workflow is about to
acknowledge in full, leaving that second worker's question permanently
unanswered under a token that's already being consumed. The pause must not
become externally visible — job record or activity return alike — until the
drain is finished and the batch is final.

Concatenating batches is not safe as-is: `hitl.convert_to_structured_questions()`
preserves a question's `id` verbatim when the caller already supplied one
(`hitl.py:234`), only auto-generating a `uuid4`-suffixed id otherwise. Two
independent workers can plausibly emit the same explicit id (e.g. both just
call their single escalated question `"q1"`), which was harmless when each
question set was scoped to one worker's own pause but collides once
concatenated: `hitl.answers_to_resolved()`'s `by_id` lookup and this
contract's own `question_task_id` mapping (§2) are both keyed by `id`, so a
duplicate silently keeps only the last worker's entry — the other worker's
answer is dropped and it can pause on the same question repeatedly. When
combining batches, the orchestrator must therefore namespace each question's
`id` by its originating task (e.g. `f"{task_id}:{original_id}"`) before
concatenating, not merely concatenate the raw per-worker batches, while still
using the same namespaced id in `pending_questions` and in the `task_id`
attribution added below — the two must agree. Leaving a
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
  consistent with how every other signal in this repo is already called).
  The handler must check `self._active_resume_token` **before** doing any
  token-equality comparison, in this order — the ordering matters, not just
  the individual rules, since the activity persists
  `waiting_for_answers=True` to the job record (and a client can act on
  that) before the activity call itself returns, so a signal can legitimately
  arrive while `self._active_resume_token` is still `None`:
  1. **`self._active_resume_token is None`** (no pause is active from this
     workflow's perspective yet — an early signal beat the activity's return):
     buffer the payload **keyed by its own `resume_token`** —
     `self._buffered_signals: dict[str, list]`, not a single slot — inserting
     only if that specific token isn't already present (first submission per
     token wins). Do **not** fall through to the mismatch check below; a
     `None` active token is not a mismatch, it's "no opinion yet." A single
     shared slot is not enough here either: a delayed retry for an
     already-resolved pause A can arrive after A clears but before pause B's
     activity even returns, occupying a lone slot and silently discarding
     B's own early, legitimate submission (or vice versa) — each pause's
     buffered answer must be independent of any other pause's, resolved or
     not.
  2. **`self._active_resume_token` is set and `payload["resume_token"] !=
     self._active_resume_token`**: a stale or duplicate submission (e.g. a
     retried HTTP call, or an answer to a pause that already resolved) — must
     be **ignored**, not applied.
  3. **Otherwise** (`payload["resume_token"] == self._active_resume_token`):
     only if `self._submitted_answers is None` (this token's first valid
     batch), set `self._submitted_answers = payload["answers"]` — a second
     matching-token signal (a double-submit, or two clients racing to answer
     the same pause) must be ignored too, not silently overwrite the first.
     Temporal can deliver both signals before the workflow task next runs and
     observes `wait_condition`, so an unconditional overwrite would make
     which human answer "wins" depend on delivery order rather than
     first-submission-wins.

  `self._submitted_answers` is the *only* state the wait condition gates on.
  Once the workflow observes "paused" and learns `resume_token`, it looks up
  (and evicts) that key in `self._buffered_signals`; **every other entry is
  discarded too, not left alone.** This workflow's main loop (below) only
  ever has one pause round in flight at a time — the moment a new
  `resume_token` activates, every other key still sitting in
  `self._buffered_signals` belongs either to a pause that already resolved
  (its own activation already evicted its entry) or to a `resume_token` that
  will *never* activate (a stale/duplicate retry's payload, buffered during
  the narrow window where `self._active_resume_token` was `None` between two
  pause rounds — see rule 1 above). Since `resume_token` is minted fresh per
  pause round and never reused, such an entry can provably never be claimed
  by any future pause; leaving it in the dict rather than dropping it here
  would let repeated stale retries across a long-running job's many pause
  rounds accumulate unboundedly in durable workflow state. Evicting
  everything but the newly-activated key on each activation bounds
  `self._buffered_signals` to at most one stale entry per pause round instead
  of growing without limit.
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
    # request["acknowledged_resume_token"] (if this isn't the first iteration)
    # has now done its job -- the just-completed activity call either
    # consumed it (matched the persisted pause it named) or ignored it (no
    # matching pause), per the §1 match rule. Clear it immediately, the same
    # way self._submitted_answers / _pending_questions / _active_resume_token
    # are reset below, rather than leaving the previous round's token sitting
    # in `request` unbounded: the §1 rule already makes a stale leftover
    # token inert on its own (it's compared for exact equality against the
    # *current* persisted pause's resume_token, and tokens are never reused,
    # so a stale value can't accidentally match a fresh pause) -- but a
    # careless reimplementation that checks only "is a token present" rather
    # than matching it would be vulnerable to exactly that, and this workflow
    # dict is the only place this field lives (it isn't itself persisted
    # anywhere). Clearing it here removes that failure mode by construction
    # instead of relying solely on every future reader implementing the §1
    # match rule correctly.
    request.pop("acknowledged_resume_token", None)
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
    # A gate on submitted answers alone silently strands the workflow if the
    # job is cancelled while paused: SE's cancel route marks the job record
    # cancelled unconditionally, then makes a BEST-EFFORT attempt at native
    # Temporal cancellation (`cancel_run_team_workflow`, api/routes/jobs.py's
    # /run-team/{job_id}/cancel) whose failure is explicitly swallowed
    # (`except Exception: logger.debug(...)`) -- so a transient RPC error
    # leaves the job record terminal while this workflow, having received no
    # cancellation, would sit in an unconditional wait_condition forever.
    # Today's hitl.wait_for_answers poll loop can't strand this way: it
    # independently re-checks the job record's terminal status on every 5s
    # tick regardless of what happened at the Temporal RPC layer. The
    # native design needs the same reconciliation, actually wired into the
    # loop rather than merely documented as a risk:
    # RECONCILE_COUNT_LIMIT bounds workflow-history growth from this loop: each
    # reconciliation tick below records a durable timer plus an activity
    # execution, and unlike worker-escalation (bounded to a short drain
    # window), an entry or Tech-Lead pause can legitimately wait on a human
    # for days. At a 60s RECONCILE_INTERVAL, a limit of 500 ticks caps this
    # loop's own contribution to history at ~8h of wall-clock reconciliation
    # before rolling into a fresh run via continue_as_new -- tune both
    # constants together against the worker's actual Temporal history limits.
    reconcile_ticks = 0
    while self._submitted_answers is None:
        try:
            await workflow.wait_condition(
                lambda: self._submitted_answers is not None,
                timeout=RECONCILE_INTERVAL,  # e.g. 60s -- bounds how stale a
                # missed-cancellation can get; independent of the §4 open
                # question on a fail-closed answer-wait timeout, which governs
                # how long a pause waits for a *human*, not how often this
                # loop reconciles against the job record
            )
        except asyncio.TimeoutError:
            # New activity: reads the job record's status only, no mutation --
            # the workflow has no direct job-store access, only activities do.
            status = await workflow.execute_activity(
                check_job_terminal_activity, request["job_id"],
                start_to_close_timeout=<short>,
            )
            if self._submitted_answers is not None:
                # A signal was processed (by the signal handler in §2) while
                # the activity call above was in flight -- the client has
                # already been told delivery succeeded (§3). Fall through to
                # resolve normally instead of treating this tick as a
                # reconciliation cycle; do NOT continue_as_new here, or this
                # already-accepted answer is silently dropped.
                break
            if status["is_terminal"]:
                # The job record, not this workflow, is authoritative for
                # cancellation (§4 decision) -- a terminal record wins even
                # though this workflow itself was never signaled or cancelled.
                return {
                    "outcome": "failed" if status["status"] != "completed" else "completed",
                    "job_id": request["job_id"],
                    "error": status.get("error"),
                }
            # Job record still active -- the timeout fired with nothing wrong.
            # If native Temporal cancellation eventually succeeds instead (the
            # common case), it interrupts wait_condition on its own via
            # Temporal's own cancellation propagation, independent of this
            # reconciliation path.
            reconcile_ticks += 1
            if reconcile_ticks >= RECONCILE_COUNT_LIMIT:
                # Re-check once more immediately before rolling the run over --
                # the window between the check above and this point is narrow
                # but not zero, and continue_as_new is irreversible once called.
                if self._submitted_answers is not None:
                    break
                workflow.continue_as_new(
                    build_continue_state(
                        request=request,
                        active_resume_token=self._active_resume_token,
                        pending_questions=self._pending_questions,
                        buffered_signals=self._buffered_signals,
                        # submitted_answers is deliberately NOT carried here:
                        # the break above already guarantees it's still None
                        # at this exact point, so the new run correctly starts
                        # back at "still waiting."
                    )
                )
                # unreachable -- continue_as_new raises internally and never
                # returns control to this frame

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
        # TODO(implementer): both `pause_cycle._format_decisions` and
        # `v2_team_worker._feedback_lines` are module-private helpers this
        # contract depends on for exact behavior parity -- couple the native
        # implementation to them only for now, and promote them to a small
        # public, stable API (e.g. a `format_decision_feedback(resolved)`
        # helper with a documented output contract) before or alongside
        # implementing this section, so a later rename/signature change in
        # either module doesn't silently break the workflow's feedback path.
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
  - Presence alone is not enough: in the native-Temporal-signal branch, the
    route must also compare the submitted token against the job record's
    currently persisted `resume_token` and reject a mismatch (e.g. 409)
    *before* calling `signal_workflow_sync`, rather than forwarding it and
    letting the workflow silently no-op. A legitimate client can only ever
    have learned a `resume_token` from the pause notification or a status
    poll, both of which read it off the job record — so by construction that
    record is already written by the time any real client could hold a
    token, and the route can validate against it directly. Skipping this
    check has two costs: the route reports success for a request the
    workflow is about to ignore, giving the caller false confidence their
    answer landed; and during the early-signal window (§2), an incorrect
    token is buffered in `self._buffered_signals` keyed by that same wrong
    value — since no real pause will ever carry it, it is never looked up
    or evicted, so repeated bad submissions accumulate indefinitely in
    durable workflow state.
- **Both coding-team and SE answers routes need a three-way mode branch**,
  not a binary Temporal/thread split — a Temporal-mode job is not
  automatically signal-capable:
  - **Native-signal-capable Temporal workflows** (`CodingTeamWorkflow`, and
    `RunTeamWorkflowV2`, unconditionally): signal without
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
    - Both routes are plain synchronous `def` handlers today
      (`coding_team_hitl.py:23`, `hitl.py:42`), and the Temporal client is
      owned by the *worker's* event loop, not the FastAPI request loop —
      `await handle.signal(...)` directly in the route is both invalid
      syntax in a sync `def` and, even made `async`, would use a client
      bound to the wrong loop. Use the existing cross-loop bridge this repo
      already built for exactly this: `signal_workflow_sync(workflow_id,
      "submit_answers", {"resume_token": client_resume_token, "answers":
      answers})` (`shared/temporal/runner.py:270-300`, which schedules
      `handle.signal` onto the worker loop via
      `asyncio.run_coroutine_threadsafe` and blocks for delivery) — a single
      dict payload, using the client-echoed token described above and
      matching this repo's single-payload signal convention.
  - **Everything else keeps today's store-and-clear behavior unchanged** —
    write `submitted_answers` and clear `waiting_for_answers` in the job
    record directly, since there is no signal handler to reach:
    - **Thread-mode jobs** (e.g. the still-threaded `run-from-github` flow),
      where `hitl.wait_for_answers`'s poll loop is exactly what the
      store-and-clear write unblocks.
    - **SE V1 Temporal jobs** — `/run-team` can no longer start new
      `RunTeamWorkflow` (V1) executions (the `SE_WORKFLOW_V2` start-path gate
      was removed); the class and its worker registration remain only for any
      still-open V1 histories (see "V1 drain status" below — none are
      currently open). Any such execution's activity still blocks in the SE
      `orchestrator.py`'s own `_wait_for_user_answers` poll loop and the
      workflow defines no `submit_answers` signal at all. Signaling a V1 job
      would target a workflow with no such handler; it must keep writing to
      the job store like today until V1 is fully removed.
    The route must check both the job's mode (thread vs. Temporal) *and*,
    for Temporal jobs, whether the actual running workflow is
    signal-capable (V2) before choosing a branch — attempting a signal
    against a workflow ID that doesn't define `submit_answers`, or that
    isn't the one actually running, silently fails to unblock it.
  - **"V2 workflow" is not itself sufficient — the check must be per-phase,
    not per-workflow-class.** `RunTeamWorkflowV2`'s Phase 1
    (`parse_spec_activity`, `temporal/workflows.py:119-127`) runs the PRA
    agent (`temporal/activities.py:448-469`), which pauses via its own
    *third*, entirely separate poll loop —
    `product_requirements_analysis_agent/user_communication.wait_for_answers`
    (`user_communication.py:88-106`), gated purely on the job-store
    `waiting_for_answers` flag and cleared by that same module
    (`user_communication.py:65-82`) — never touched by this redesign, which
    only restructures the coding-team-specific pauses inside
    `run_pipeline_activity` / `execute_coding_team_activity`'s Phase 3. A
    pause that occurs during a `RunTeamWorkflowV2` job's Phase 1 is
    therefore still a PRA-phase pause with no `submit_answers` signal
    wired for it at all; routing it to the signal-only branch just because
    the workflow class is V2 would buffer a signal nobody reads while the
    real PRA poll loop keeps blocking on a job-store flag this branch was
    told not to clear. The route must instead check what's actually
    persisted for *this* pause: only a pause carrying the new discriminated
    envelope (`resume_token` / `pause_kind`, per §1 — i.e. one that went
    through the redesigned Phase 3 activity) takes the signal-only branch;
    a pause with no such envelope (PRA's Phase 1, or SE V1 entirely) takes
    the store-and-clear branch regardless of the workflow's class.
- **Current status (start-path gate removed):** `/run-team` always starts
  `RunTeamWorkflowV2` — there is no longer an operator opt-out back to V1.
  This does **not** change HITL behavior described above — V2's Phase 3
  activity (`execute_coding_team_activity`) still calls
  `run_coding_team_orchestrator` with the default `pause_strategy="block"`,
  not `"return"`, so `/run-team/{job_id}/answers` continues to resume V2 jobs
  exactly as it resumed V1 jobs before: by writing to the job store and
  relying on `orchestrator._wait_for_user_answers`'s poll loop, with no
  Temporal signal involved. The native-signal branch described in this
  section (`RunTeamWorkflowV2` gaining a `submit_answers` signal, the
  discriminated `resume_token`/`pause_kind` envelope, the per-phase check)
  remains unimplemented future work, not something removing the gate
  requires or provides.
- **V1 drain status (2026-08-10):** no managed environment currently runs
  the SE Temporal worker, so no in-flight `RunTeamWorkflow` (V1) executions
  exist anywhere today — there is nothing to drain, complete, or cancel.
  **Go/no-go for deleting V1 (5.3/5.4): GO**, with one contingency — if a
  managed environment (staging/prod with `TEMPORAL_ADDRESS` set) is stood up
  before 5.3/5.4 land, whoever deploys it must first re-check for open V1
  executions (Temporal UI/CLI visibility query:
  `WorkflowType = 'RunTeamWorkflow' AND ExecutionStatus = 'Running'` against
  the `software-engineering` task queue) and drain/cancel any found before
  the `RunTeamWorkflow` class and its worker registration are removed.
- `POST /run/{job_id}/resume` is Temporal-native only: it requires a
  `resume_token` and `waiting_for_user`, then signals `CodingTeamWorkflow`.
  The old cross-worker claim lease (`resume_claim_at` / `resume_claim_seq`)
  and thread-spawn resume path are deleted — Temporal itself durably tracks
  a waiting workflow across worker restarts.
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
  - **Nor does setting a bare `request["acknowledged_resume_token"]` reach the
    standalone `CodingTeamWorkflow` path either.** `run_pipeline_activity`
    validates its payload as `RunRequest` (fields: `repo_path`, `plan_input`
    only — `api/coding_team_models.py:28-35`) and calls
    `run_orchestrator_wired(job_id, repo_path, plan)`
    (`api/orchestration.py:57`), neither of which has anywhere for an
    acknowledgement to land; an extra top-level request key is simply
    dropped by `RunRequest(**request)` parsing, just as `resolved_answers`
    was in an earlier draft of this contract. `RunRequest` must gain its own
    `acknowledged_resume_token: Optional[str] = None` field, and
    `run_pipeline_activity` must forward it through `run_orchestrator_wired`
    into the orchestrator re-entry check in §1 — without this, the
    standalone path's every normal resume is indistinguishable from a
    pre-work activity retry and re-pauses indefinitely.

## Decided: source-of-truth ownership

Earlier drafts of this contract left ownership open, but everything in §§1-3
above — the idempotent-retry rule, early-signal buffering, the
`acknowledged_resume_token` protocol, the mode-branched answers routes, and
`resume_token` in status responses — is written assuming one specific answer,
so it is decided here rather than left ambiguous: **the job-store record
stays authoritative for pause state.** The workflow's `submit_answers`
signal and `pending_questions`/`status` query are thin, workflow-local
proxies (buffered/gated state for correctness across the pause boundary),
not an alternate source of truth — the job record is what a retried
activity, a polling REST client, and the existing thread-mode/audit surface
all agree on. A workflow-owned alternative (durable workflow state as the
source of truth, job-store as an async mirror) is a materially different
design that would invalidate every mechanism above; if a future sub-issue
wants to pursue it, that is a new contract, not an extension of this one.

## Open questions (flagged, not resolved by this spike)

1. **`wait_condition` timeout.** Does it get a timeout mirroring today's
   fail-closed `hitl.answer_wait_timeout_s()` (timeout → job fails), or does
   it wait indefinitely and rely on some other mechanism (e.g. a workflow
   timer signal) for staleness handling?
2. **Round-trip granularity.** One signal round-trip per pause point (matches
   today's three independent `_run_pause_cycle` call sites), or batch
   multiple pending questions into a single richer payload? Recommendation:
   one round-trip per pause point — the entry gate, Tech Lead clarify loop,
   and per-worker escalation are already independent call sites with
   different resume semantics, and worker-level escalation in particular is
   concurrent with other in-flight work.
3. **GitHub-hook flow.** Should posting the pause as a GitHub issue comment
   (`run-from-github`'s `on_pause` callback, `hitl._format_questions_comment`)
   move into an activity invoked right after the workflow observes
   `"paused"` (so it's retryable/durable like other Temporal activities), or
   stay driven inline from the orchestrator as it is today?
4. **Duplicate poll loops.** `hitl.py`, SE `orchestrator.py`'s
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
