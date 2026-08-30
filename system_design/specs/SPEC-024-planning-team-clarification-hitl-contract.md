# SPEC-024: Planning Team Clarification-Question Temporal Signal/Wait Contract

| Field        | Value                                                                 |
|--------------|-----------------------------------------------------------------------|
| **Status**   | Proposed (design only — no production code in this story)             |
| **Author**   | Platform Engineering                                                  |
| **Created**  | 2026-08-29                                                            |
| **Priority** | P0 (blocks #7445-B / #7446)                                           |
| **Scope**    | `planning_team` Temporal workflow/activity boundary only; defines the contract, does not implement it |

> **Story note.** This spec is the output of issue #7451, the first story in the #7445 sequence
> ("Add a Temporal-durable answer-callback primitive for Planning clarification questions"). Its
> acceptance criteria require a written interface spec and explicitly forbid merging production
> code in this story. #7445-B implements the primitive this document defines; #7446 wires it into
> `temporal/activities.py`.

---

## 1. Problem Statement

`planning_team` can surface clarification questions (`OpenQuestion` /
`backend/agents/planning_team/models.py:216-244`) but has no way to actually pause a running job
and wait for a human to answer one. `resolve_pra_answers()`
(`backend/agents/planning_team/orchestrator.py:45-81`) always auto-picks the `is_default` (or
first) option whenever no `answer_callback` is supplied — and every call site, in both thread mode
(`orchestrator.py` `run_workflow`) and Temporal mode (`temporal/activities.py:320-324`,
`document_production_activity`), calls it with `auto_answer_questions=True` and no callback. A
stub answers route already exists (`api/main.py:389-424`, `POST /{job_id}/answers`) but always
returns `400`, because nothing ever sets `waiting_for_answers=True` on a Planning job record.

Temporal activities cannot natively suspend mid-execution for an arbitrary human response time —
only a *workflow* can await a signal. So before any implementation work starts, this story fixes
the shape of the solution: what the activity returns when it needs input, what the workflow waits
on, what the human's answer looks like on the wire, and how re-invocation is made idempotent.

The coding team already solved this exact problem for its own Tech-Lead clarification loop
(`SPEC-023-coding-team-human-in-the-loop.md`, `backend/agents/software_engineering_team/hitl.py`,
`pause_cycle.py`, `temporal/coding_team_workflow.py`). SPEC-023 §4.3.3 and §7 explicitly deferred
Planning's Temporal-mode pause semantics as future work. This spec is that follow-up, and its
central design decision is: **reuse that pattern verbatim rather than inventing a second one.**

---

## 2. Current State

```mermaid
flowchart TD
    Start["PlanningWorkflow.run()<br/>temporal/workflows.py"] --> DP["document_production_activity<br/>temporal/activities.py:285-377"]
    DP --> PRA["PRA raises OpenQuestion[]"]
    PRA --> Resolve["resolve_pra_answers(questions, None, True)<br/>orchestrator.py:45-81"]
    Resolve --> Auto["auto_answer_questions=True:<br/>auto-picks is_default / first option<br/>NEVER PAUSES"]
    Auto --> Continue["document_production continues<br/>against an answer no human saw"]

    Start -.->|"dead stub"| Stub
    Stub["POST /{job_id}/answers<br/>api/main.py:389-424<br/>always 400 — waiting_for_answers<br/>is never set to True"]:::dead

    classDef dead fill:#999,stroke:#666,color:#fff
    style Auto fill:#f99,stroke:#c00,color:#000
```

`PlanningWorkflow` (`temporal/workflows.py`) is a plain sequential chain of
`workflow.execute_activity` calls — intake → discovery → requirements → optional market_research →
synthesis → **document_production** → sub_agent_provisioning → finalize. No `@workflow.signal`, no
`workflow.wait_condition` exists anywhere in it today.

### Reusable machinery (preserve, do not reinvent)

- `_ActivityPauseSignal` (`pause_cycle.py:36-74`) — internal exception carrying
  `{resume_token, pause_kind, pause_context, pending_questions}`, unwound at the orchestrator
  boundary into a `{"outcome": "paused", ...}` return value.
- `mint_resume_token(job_id)` (`pause_cycle.py:77-92`) — `f"{job_id}:{uuid4().hex[:12]}"`, minted
  once per pause round.
- `_check_pending_pause_reentry(job_data, acknowledged_resume_token)`
  (`pause_cycle.py:142-177`) — classifies a re-invocation as `consume=True` (token matches →
  resume) or `consume=False` (token missing/mismatched → activity retry, re-emit the same paused
  payload, do no new work).
- `submit_answers` signal (`temporal/coding_team_workflow.py:282-348`) — name, payload shape, and
  the buffering state machine (`_active_resume_token`, `_submitted_answers`,
  `_buffered_signals`) on `CodingTeamWorkflow`.
- `backend/shared/hitl/models.py` — `PendingQuestion`, `QuestionOption`, `AnswerSubmission`,
  `SubmitAnswersRequest`: the team-agnostic superset schemas, already built for exactly this kind
  of cross-team reuse.

---

## 3. Goals and Non-Goals

**Goals**
- Define the Temporal signal name and payload for delivering a human's answers to a Planning
  clarification question.
- State unambiguously which side (workflow vs. activity) owns the wait, and why.
- Define the retry/continuation shape when a clarification question is raised mid-activity.
- State explicitly how this reuses `hitl.py`/`pause_cycle.py` rather than diverging from it.
- Define Preconditions/Postconditions/Invariants the eventual primitive (#7445-B) must satisfy.

**Non-Goals** (deferred to later stories)
- Implementing the mechanism (#7445-B).
- Wiring `document_production_activity` / `resolve_pra_answers` to actually use it (#7446).
- Thread-mode (non-Temporal) pause behavior — `shared/temporal/checkpoints.py`'s `wait_for_input`/
  `submit_input` already cover that path per `shared/temporal/README.md`; this spec covers only
  the Temporal-native signal.
- The full REST/UI surface (routes, request validation) beyond the one field this spec's contract
  requires exposing — see §4.1's `resume_token`-delivery requirement below; that field's existence
  is in scope precisely because without it a polling client has no way to learn the token
  `SubmitAnswersRequest.resume_token` asks it to echo back.

---

## 4. Detailed Design

### 4.1 Signal name and payload

Reuse the coding team's signal **verbatim** — same name, same shape:

```python
@workflow.signal(name="submit_answers")
def submit_answers(self, payload: dict[str, Any]) -> None:
    ...
```

Payload: `{"resume_token": str, "answers": list[dict]}`, where each `answers` element is
`AnswerSubmission`-shaped (`backend/shared/hitl/models.py:70-77`):
`{"question_id": str, "selected_option_id": Optional[str], "other_text": Optional[str]}` —
**extended** with a plural field, `"selected_option_ids": List[str]` (default `[]`), required for
`allow_multiple=True` questions (below).

**Contract requirement — a polling client must be able to learn the `resume_token` in the first
place.** `SubmitAnswersRequest.resume_token` (`shared/hitl/models.py:80-96`) is where the client
*sends* the token back, but Planning's current client-facing status surface —
`PlanningStatusResponse` (`planning_team/models.py:120-131`) and `get_status`
(`api/main.py:318-329`) — exposes only `pending_questions`/`waiting_for_answers`, with no field
carrying the token itself; the paused activity's `resume_token` (§4.1's payload above) is consumed
entirely inside `PlanningWorkflow`/the job record today. Without a client-visible field, no caller
can ever populate `SubmitAnswersRequest.resume_token` correctly. This mirrors the coding team's own
`StatusResponse.resume_token` (`software_engineering_team/api/coding_team_models.py:106-112`) —
required here for the same reason. **Contract requirement:** extend `PlanningStatusResponse` with
`resume_token: Optional[str] = None`, populated (from the job record's persisted `resume_token`)
whenever `waiting_for_answers` is `True`, `None` otherwise — the client-visible half of the same
pause envelope §4.3.1 already persists server-side.

**Contract requirement — carry every selection, not just one.** PRA's own `OpenQuestion`
(`product_requirements_analysis_agent/models.py:78`, mirrored in
`planning_team/models.py:224`) sets `allow_multiple=True` on some questions, and Planning's own
`AnsweredQuestion` model already has both `selected_option_id` *and* `selected_option_ids: List[str]`
(`planning_team/models.py:241-242`) for exactly this reason. `shared/hitl/models.py`'s
`AnswerSubmission`, reused verbatim above, currently has **only** the singular field — reusing it
as-is for Planning would silently drop every selection but one on a multi-select question. This
contract requires extending `AnswerSubmission` with an optional `selected_option_ids: List[str] =
Field(default_factory=list)`, populated instead of (not in addition to, for that question)
`selected_option_id` when the source question has `allow_multiple=True`. This is not
Planning-specific scope creep: PRA's own answers-submission route
(`software_engineering_team/api/routes/product_analysis.py:283`) forwards only
`selected_option_id` today, from the same shared model — so this gap already exists for any
`allow_multiple` PRA question, coding-team or Planning. #7445-B/#7446 must land the
`AnswerSubmission` field addition *and* the corresponding pass-through at
`api/routes/product_analysis.py:283` together; shipping Planning's pause primitive without it would
build a new, correctly-plumbed pause/resume path on top of a wire format that still can't carry a
multi-select answer through to PRA.

**The shared validator needs the same fix, not just the model.** Adding the field to
`AnswerSubmission` is necessary but not sufficient: `shared.hitl.validation.validate_answers`
(`shared/hitl/validation.py:81-118`, the coding team's own answer-validation entry point, and the
natural one for Planning's answer-submission route to reuse rather than duplicate) checks only
`a.selected_option_id`/`a.other_text` and — at line 100 — rejects any answer where both are falsy
as "not a decision," with no awareness of `selected_option_ids` at all; its returned dict
(`validation.py:110-118`) doesn't carry the plural field either, so even an answer that somehow
passed validation would have its multi-select content silently dropped before it ever reaches the
job store. **Contract requirement:** `validate_answers` must recognize a populated
`selected_option_ids` as a valid decision (validating each id against the question's own options,
mirroring the singular-field check at lines 92-99), and must include `selected_option_ids` in the
dict it returns. Without this, every compliant multi-select submission — through the coding team's
existing route or any Planning route that reuses this validator — is rejected with a 400.

**Why the same name, not `planning_submit_answers` or similar:** `backend/shared/hitl/models.py`
was deliberately built as a cross-team superset so both teams share one vocabulary. A single
signal name across teams means any future workflow that hosts both a coding-team-style gate and a
planning-style gate (SPEC-023 §4.3.3 flags `RunTeamWorkflowV2` as exactly this case) needs no
signal-name disambiguation. Each `PlanningWorkflow` instance is its own Temporal workflow run, so
there is no name collision risk within one workflow's signal namespace — reuse costs nothing here.

The activity's paused-return payload, symmetric with the coding team's:

```python
{
    "outcome": "paused",
    "job_id": str,
    "resume_token": str,               # mint_resume_token(job_id)
    "pause_kind": "planning_clarification",
    "pause_context": None,             # Planning has one clarification gate per job, no per-task
                                        # sub-context analogous to coding-team worker escalation
    "pending_questions": [...],        # PendingQuestion-shaped dicts, converted from OpenQuestion
}
```

`pause_kind` is a **new** value (`"planning_clarification"`), not one of the coding team's three
(`entry` / `tech_lead_clarify` / `worker_escalation`) — Planning has exactly one clarification
gate (the `document_production_activity` phase where PRA raises questions), so one kind is
sufficient; it need not fit the coding team's per-source taxonomy. `pause_context` is `None`
because Planning has no sub-task identifier equivalent to the coding team's `task_ids` — the whole
job is what's paused.

### 4.2 Which side owns the wait

**The workflow (`PlanningWorkflow`) owns `workflow.wait_condition`.** Activities cannot pause —
`document_production_activity` must return promptly with the `outcome: "paused"` dict the moment
PRA reports unanswered questions, exactly as `run_pipeline_activity` does today
(`temporal/coding_team_workflow.py:105-247`).

`PlanningWorkflow` gains the same three instance fields as `CodingTeamWorkflow`
(`temporal/coding_team_workflow.py:277-280`):

```python
self._active_resume_token: str | None = None
self._submitted_answers: list[dict[str, Any]] | None = None
self._buffered_signals: dict[str, list[dict[str, Any]]] = {}
```

and the identical signal-handler rules (`temporal/coding_team_workflow.py:282-348`):
- No active pause yet → buffer the payload under its own `resume_token` in `_buffered_signals`
  (an early signal beat the workflow arming the wait).
- Active pause but token mismatch → ignore (stale/duplicate).
- Active pause, matching token, first submission → set `_submitted_answers` (the sole
  `wait_condition` predicate).
- Arming a new pause consumes any matching buffered entry immediately and discards every other
  buffered entry (bounds memory across pause rounds).

This is a **copy, not a redesign** — the state machine is proven (see the integration tests listed
in §6) and Planning's clarification gate has no property that would require a different one.

### 4.3 Retry/continuation shape

`PlanningWorkflow.run` wraps the `document_production_activity` call in the same loop shape as
`CodingTeamWorkflow.run` (`temporal/coding_team_workflow.py:546-579`):

```python
while result.get("outcome") == "paused":
    resume_token = result.get("resume_token")
    if not isinstance(resume_token, str) or not resume_token:
        # Mirrors CodingTeamWorkflow.run's own guard (temporal/coding_team_workflow.py:552-563):
        # a paused result is contractually guaranteed to carry a resume_token; fail fast and
        # deterministically here rather than let wait_condition's predicate become permanently
        # unsatisfiable.
        raise ValueError(f"Paused activity result missing a valid resume_token: {result!r}")
    self._active_resume_token = resume_token
    self._submitted_answers = self._buffered_signals.pop(resume_token, None)
    self._buffered_signals.clear()
    await workflow.wait_condition(lambda: self._submitted_answers is not None)
    request["acknowledged_resume_token"] = self._active_resume_token
    self._submitted_answers = None
    self._active_resume_token = None
    result = await workflow.execute_activity(
        document_production_activity, request, start_to_close_timeout=activity_timeout
    )
    request.pop("acknowledged_resume_token", None)
```

`document_production_activity` becomes idempotent on re-entry using
`_check_pending_pause_reentry` (`pause_cycle.py:142-177`) unchanged:
- No persisted pause on the job record → proceed normally.
- `acknowledged_resume_token` matches the persisted token → genuine resume; **apply the
  now-answered questions first** via `resolve_pra_answers(..., answer_callback=<from job record>)`
  and confirm PRA has durably accepted them, **then** clear the pause envelope (never the reverse —
  see the crash-safe ordering requirement below, normatively defined in §5's resume-path
  postconditions), and continue past the point PRA raised them.
- Token missing/mismatched but a pause is persisted → this is a pre-work activity retry (e.g.
  Temporal retried the activity after it persisted-but-not-yet-returned the pause); re-emit the
  exact same `{"outcome": "paused", ...}` payload unchanged, doing no new PRA work.

**Answers must be persisted before the signal, not carried by it.** The `submit_answers` payload
(§4.1) is the *wake-up*, not the sole record of the answer — mirroring
`coding_team_hitl.submit_pending_answers` (`api/routes/coding_team_hitl.py:20-71`) exactly: that
route calls `_main.store_append_submitted_answers(job_id, answers)` (persisting the validated
`AnswerSubmission` list to the job record) *before* it calls `signal_workflow_sync(..., "submit_answers",
{"resume_token": resume_token, "answers": answers})`. The workflow-side loop in this section
deliberately drops `self._submitted_answers` after `wait_condition` returns (it only forwards
`acknowledged_resume_token`, matching `CodingTeamWorkflow.run` field-for-field) — the resumed
activity is expected to read the answers back from the job record, not from the signal payload.
Planning's answer-submission path (whatever replaces the currently-stubbed `POST
/{job_id}/answers`, `api/main.py:389-424`) MUST perform the same "persist-then-signal" write —
appending to a `submitted_answers` job-record field — before delivering the `submit_answers`
signal; this is a required part of the contract, not an implementation detail #7445-B is free to
skip. Without it, `resolve_pra_answers(..., answer_callback=<from job record>)` on resume has
nothing to read and the resume path silently regresses to auto-answering.

**The persist step must be first-write-wins, not last-write-wins.** Two clients can race to submit
answers for the same `resume_token` (a stale UI retry, a double-click, a legitimate second
attempt). The workflow's signal handler already commits to "first submission wins, everything else
for that token is ignored" (§4.2) — but that rule lives entirely in workflow memory (`_submitted_answers`,
set once). If the job-record write is a blind overwrite, a slower client's write can land *after*
a faster client's signal already woke the workflow, so the resumed activity re-reads a batch that
does not correspond to whichever signal the workflow actually accepted — the two "first" decisions
(signal-layer, store-layer) can disagree. **Contract requirement:** the job-record write for a
given `resume_token`'s answer batch must be an atomic compare-and-set / write-once operation —
only the first successful write for that token is retained; every subsequent write attempt for an
already-populated token from *different* content must be rejected (and its signal, if the client
sends one anyway, is already a harmless no-op on the workflow side per §4.2's duplicate-token
rule). This makes "which batch does the resumed activity read" and "which submission attempt
actually won" the same question by construction, regardless of which client's `submit_answers`
signal happens to reach the workflow first — the workflow's signal-acceptance order need not (and
cannot, without this) be made to agree with the store's write order any other way.

**A rejected write must not strand the winner's own retry.** The write and the signal are two
separate operations (§4.3's persist-then-signal ordering); a client can have its write succeed and
then have `signal_workflow_sync` itself fail (a transient Temporal outage, a network blip) *before*
the signal is delivered — the answer is durable, but the workflow is still asleep. If that same
client retries its whole submission and the write-once rule above rejects it outright (because the
token is already populated — by its own prior write), a client that gives up on rejection strands
the job forever: durable answer, no signal, no further retry. **Contract requirement:** the
write-once check must compare the retry's content against what's already persisted for that token
— identical content (the winner retrying its own submission) is not a conflict and must be treated
as a no-op *on the write* that proceeds straight to (re-)delivering the signal; only genuinely
different content for an already-populated token is rejected. Practically, this means the
answer-submission route always attempts to (re-)signal after the write step, whether that write
step performed a fresh write or recognized its own prior one — never conditioning "do we signal"
on "did this specific call perform the write." Re-signaling an already-applied token is safe
regardless: §4.2's signal handler ignores a signal that doesn't match an active pause and, once
consumed, ignores a duplicate for the same token.

**Persisted answers must be scoped to the active question round, not accumulated across rounds.**
Unlike the coding team's single Tech-Lead clarify loop, a single `document_production_activity`
run can pass through PRA's `wait_pra` poll loop (`adapters/product_analysis.py:80-106`) more than
once if PRA raises more than one round of questions before completing — each `_on_poll` invocation
calls `answer_callback(pending)` again for whatever `pending_questions` PRA reports *at that
moment*. If `submit_answers` naively **appends** every batch to one flat `submitted_answers` list
and the resume-path callback naively returns the *entire* accumulated list, a later round's
callback re-submits an earlier round's already-consumed `question_id`s alongside the new ones.
PRA's own answers endpoint (`POST .../product-analysis/{job_id}/answers`) rejects a submission
containing an id outside its *current* `pending_questions`; `submit_product_analysis_answers`'s
response is not checked by `_on_poll` (`adapters/product_analysis.py:96-97`) and
`wait_for_product_analysis_completion` has no failure branch for it — it simply keeps polling,
so a rejected resubmission degrades to a silent hang until `MAX_POLL_WAIT` (3600s) expires, not a
clean error.

**Contract requirement:** the answer-submission path must tag each persisted answer batch with the
`resume_token` of the pause round it resolves (not just append to one undifferentiated list), and
`resolve_pra_answers(..., answer_callback=<from job record>)` on resume must filter to *only* the
batch whose `resume_token` matches the round currently being resumed — equivalently, only answers
whose `question_id` is a member of that round's persisted `pending_questions`. Once a round's
batch has been consumed by a resume, it must be marked consumed (or moved into
`resolved_questions`, which `HandoffPackage` already carries — `planning_team/models.py:193-207`)
so a later round's callback never sees it again. This is the same token-scoping discipline §4.2
already requires of the workflow's signal handler, applied here to the job-record answer store the
activity reads from.

**Recommended shape (decision for #7445-B, not mandated here):** store `submitted_answers` keyed
by `resume_token` — `{"<resume_token>": [AnswerSubmission, ...]}` — rather than one flat list, so
"only this round's answers" is a single dict lookup rather than a filter over question ids that
could theoretically collide across rounds.

**Recommended extraction (decision for #7445-B, not mandated here):** `mint_resume_token` and
`_check_pending_pause_reentry` have no coding-team-specific logic — they operate purely on a job
record dict and a resume token. #7445-B should consider extracting them from
`software_engineering_team/pause_cycle.py` into a new `backend/shared/hitl/pause_cycle.py`
(alongside the existing `shared/hitl/models.py`), so Planning **imports** the primitive rather than
duplicating it or reaching across team boundaries into `software_engineering_team`. This spec does
not mandate the extraction's exact shape — only that Planning's implementation must not diverge in
behavior from what's documented in §4.4 below, however the code ends up organized.

### 4.3.1 Checkpointing the external PRA run before pausing

The coding team's pause cycle unwinds a stack frame entirely internal to the activity's own
process — nothing external needs to be told "this is still the same run." Planning's clarification
gate is different: `document_production_activity` → `run_document_production` →
`DocumentProductionAgent.run` (`agents/document_production/agent.py:91-94`) calls
`job_id = run_pra(repo_path=..., spec_content=...)` to **submit a new external Product Requirements
Analysis job**, then `wait_pra(job_id=job_id, answer_callback=...)` polls it. If
`document_production_activity` is simply re-invoked from the top on resume — the way
`run_pipeline_activity` is for the coding team — it re-executes `run_document_production` from
scratch, which calls `run_pra(...)` again: a **second** external PRA job is submitted, the
original (already-answered) PRA job is stranded, and PRA-side side effects are duplicated. This is
a real gap the coding-team pattern does not have to solve and this contract must.

**Contract requirement — checkpoint eagerly, not at the pause point.** The checkpoint must be
written the moment the external PRA job id is obtained — immediately after `run_pra(...)` returns,
**before** `wait_pra(...)` is ever called — as its own atomic `update_job` call via
`shared/temporal/checkpoints.py`'s `save_checkpoint("planning_team", planning_job_id,
"document_production_pra", {"pra_job_id": pra_job_id})` (`shared/temporal/README.md`
§"Checkpoints and human-in-the-loop"). This is a deliberate change from binding the checkpoint to
the pause envelope: PRA may run to completion **without ever pausing** (no clarification needed),
so a checkpoint written only at the pause point would never exist on the un-paused path, and a
worker crash *before* any pause is reached (activity retried, or the workflow re-invokes with no
`acknowledged_resume_token` at all) would still resubmit PRA with no checkpoint to prevent it.
Writing the checkpoint unconditionally and immediately closes that gap regardless of whether this
run ever pauses.

Symmetrically, **every entry into `document_production_activity`** — a fresh run, a Temporal-level
activity retry, or a workflow-driven resume — must `load_checkpoint("planning_team",
planning_job_id, "document_production_pra")` *before* deciding whether to call `run_pra(...)`: a
present checkpoint means "PRA already submitted for this job," full stop, independent of whether a
pause envelope also happens to be persisted. When present, call `wait_pra(job_id=<checkpointed
pra_job_id>, answer_callback=...)` directly — **never** call `run_pra(...)` again for this job. This
makes checkpoint-presence, not the pause envelope, the single source of truth for "already
submitted," and removes any need for the checkpoint and pause-envelope writes to be one atomic
update — the checkpoint alone is sufficient to prevent resubmission at every re-entry, no matter
which write reached durable storage last. The pause envelope's own write (`waiting_for_answers`,
`resume_token`, `pause_kind`, `pause_context`, `pending_questions`) remains a separate atomic
`update_job` call, made only once PRA actually raises unanswered questions, and continues to be
read via `_check_pending_pause_reentry` (§5) exactly as before.

Note the two distinct ids throughout: `planning_job_id` (the Planning job this activity/workflow is
running for — what `job_id`/`load_checkpoint`/`save_checkpoint` key on) and `pra_job_id` (the
external PRA job id returned by `run_pra(...)`, carried only inside the checkpoint payload). The
checkpoint must be read/written via the Planning job-store's own team namespace, `"planning_team"`
— matching `get_job_service_client(team="planning_team")` in
`planning_team/shared/job_store.py:25` — **not** `"planning"`; using the wrong team argument
addresses a different job-service partition and the checkpoint would never be visible from the
Planning job record on any later read.

This requires `run_document_production` / `DocumentProductionAgent.run` to accept an optional
pre-existing PRA `job_id` and skip submission when one is supplied (implementation shape for
#7445-B; the contract here is only that resubmission must not happen, ever, once a checkpoint
exists).

**Residual risk this alone does not close — the crash window between `run_pra` returning and the
checkpoint write landing.** Writing the checkpoint "immediately after `run_pra` returns" narrows
the unprotected window to a single in-process gap, but does not eliminate it: a worker can still
die after `run_pra(...)` has created the external job and before `save_checkpoint(...)` durably
persists its id. `run_product_analysis` (`adapters/product_analysis.py:33-48`) is a plain POST with
no idempotency key and no reconciliation lookup, so on retry the activity finds no checkpoint and
submits a second PRA job — genuinely indistinguishable, from Planning's side, from a first
submission. **Closing this completely requires PRA's own `/product-analysis/run` endpoint to accept
a client-supplied idempotency key** (or expose a way to look up an existing job by one), which is
outside `planning_team`'s boundary and this spec's stated scope — it is software_engineering_team's
endpoint. This spec cannot mandate a fix on the other side of that boundary; it can only avoid
making the gap worse and say plainly what closes it.

**Contract requirement given that constraint:** until PRA supports an idempotency key, the PRA
*submission* itself (`run_pra(...)` through the eager `save_checkpoint` write, and nothing else)
must be its own **separately-scheduled Temporal activity**, executed via its own
`workflow.execute_activity(..., retry_policy=NO_RETRY)` call — not the bounded/default retry
policy recommended below for the rest of the activity. This must be a genuinely distinct
`execute_activity` invocation, not an in-process "do not retry past this point" guard inside the
larger `document_production_activity` function: Temporal attaches a retry policy to an entire
`execute_activity` command, not to a region of code within one activity function, so if
`document_production_activity` as a whole runs under the permissive policy §4.3.1 requires for
`wait_pra`/pause/resume, a worker crash between `run_pra()` returning and the checkpoint write is
retried under that *same* permissive policy regardless of any in-process guard — the guard cannot
stop Temporal's own retry of the whole function from the top. Only a separate `NO_RETRY` activity
for the submission step actually gets Temporal to treat that step as non-retryable. A crash in that
narrow window then fails the workflow cleanly (loud, visible, needing a human/operator to reconcile
or restart the job) rather than silently duplicating a PRA job. Everything *after* a checkpoint
exists — `wait_pra` polling, pause, resume, and the rest of `document_production_activity` — is
safe to retry freely in its own (different) activity invocation, because those steps only ever
consult the checkpoint and never resubmit. The contract fixes the retry-policy asymmetry (a
dedicated `NO_RETRY` submission activity vs. a retryable activity for everything after) and flags
PRA-side idempotency as the complete fix, tracked as an open item alongside the unbounded-
`wait_condition` risk already carried in this
section (below).

**Contract requirement — the activity must actually be retryable (past the submission step), under
a *bounded* policy specifically.** `PlanningWorkflow`'s current
`workflow.execute_activity(document_production_activity, ..., retry_policy=NO_RETRY)`
(`temporal/workflows.py:189-195`) means a worker crash mid-activity fails the whole workflow rather
than letting Temporal re-invoke the activity — unlike the coding team, whose
`workflow.execute_activity(run_pipeline_activity, request, start_to_close_timeout=activity_timeout)`
(`temporal/coding_team_workflow.py:546-551, 574-578`) passes **no** `retry_policy` override at all,
so it runs under the SDK's unbounded default retryable policy. Because this contract makes the
activity idempotent on re-entry — past the submission step above — via the eager checkpoint and
`_check_pending_pause_reentry` (§5), the rest of `document_production_activity` must adopt a
retryable posture too — but **must use `SAFE_RETRY` (`temporal/workflows.py:53-54`) specifically,
not the SDK's unbounded default**, unlike the coding team's activity. This is a deliberate
divergence, not an oversight: every `planning_team` activity, this one included, runs through the
`_guarded` wrapper (`temporal/activities.py:69-118`, thin over
`shared.temporal.activity_helpers.guarded`), whose contract requires `max_attempts` to be an
explicit finite value *matching the phase's own Temporal `RetryPolicy`* (`activities.py:87-88`) —
it uses that number to decide `is_final_attempt` and mark the job FAILED only on the truly last
attempt. An unbounded default policy has no finite `max_attempts` to hand `_guarded`: passing
`RETRYABLE_MAX_ATTEMPTS` (or any other finite guess) while Temporal itself retries without limit
would mark the job FAILED after that many attempts even though Temporal keeps trying past it — a
false-terminal failure — while no finite value can correctly represent "unlimited" to `_guarded`
either way. `SAFE_RETRY`'s existing finite `maximum_attempts` (already used, and already correctly
paired with `_guarded`, by every other retryable phase in this same workflow) is required; the
coding team's unbounded-default choice does not transfer here because
`software_engineering_team`'s activities have no equivalent `_guarded`-style finite-attempt-count
contract to violate. Blanket `NO_RETRY` must not remain on `document_production_activity` once the
pause contract lands, but the replacement must be `SAFE_RETRY`, not "no override at all."

**Naming, to keep the two activities and their retry policies straight:** this contract splits what
was one `document_production_activity` into two Temporal activities scheduled separately by
`PlanningWorkflow`: a small `document_production_pra_submit_activity` (illustrative name — #7445-B
picks the actual one) that takes the same `job_id`/`context` the current single activity does
(§5's precondition — it needs `repo_path`/`spec_content` to call `run_pra`) and does only
`run_pra(...)` + the eager checkpoint write, scheduled with `retry_policy=NO_RETRY`; and `document_production_activity` itself, which always starts by loading
the checkpoint (never calling `run_pra` directly), then runs `wait_pra`/pause/resume/finalize under
`SAFE_RETRY` (specifically — not the SDK default; see the `_guarded`-compatibility requirement
below). The workflow calls the submit activity once per job (a no-op
skip if a checkpoint already exists — safe to call again on workflow-level retry, since it just
re-checks the checkpoint) before entering the `document_production_activity` retry/continuation
loop of §4.3.

**Conditional on `use_product_analysis` — none of this pause machinery applies when PRA is
disabled.** `document_production_activity`'s existing `use_product_analysis: bool` parameter
(`temporal/activities.py:289`) already lets a caller opt out of PRA entirely; when `False`,
`run_document_production` never calls `run_pra`/`wait_pra` at all (`phases/document_production.py:147-159`
passes `run_pra=None`, and `DocumentProductionAgent.run`'s `if use_product_analysis and run_pra and
wait_pra:` guard, `agent.py:91`, is simply never true). The workflow must call
`document_production_pra_submit_activity` **only when `use_product_analysis` is `True`** for this
job — calling it unconditionally would submit an external PRA job for a caller that explicitly
opted out. Symmetrically, `document_production_activity`'s own precondition that a checkpoint
already exists (§5) applies only on the `use_product_analysis=True` path; when `False`, there is no
checkpoint to load, no `wait_pra` to resume, and no pause round ever begins — the activity runs
exactly as it does today, unaffected by this contract.

**Postcondition this adds to §5:** `document_production_pra_submit_activity` runs under
`NO_RETRY` and either finds an existing checkpoint (no-op) or performs exactly one `run_pra` call
followed immediately by the checkpoint write; `document_production_activity` never calls `run_pra`
itself, only `load_checkpoint`, and runs under a retry policy that actually permits Temporal to
re-invoke it after a worker crash (not `NO_RETRY`), since retry-then-reentry past the checkpoint is
the mechanism this contract relies on for crash recovery there.

**The pause signal must be caught inside `_guarded`'s `work` callable, never let reach `_guarded`
itself.** Every `planning_team` activity, `document_production_activity` included, runs its actual
work through `_guarded(job_id, phase, ..., work, max_attempts=...)`
(`temporal/activities.py:69-118`), which wraps `work()` in `try: ... except Exception as exc: if
is_final_attempt(max_attempts): fail_job(...); raise` (`shared/temporal/activity_helpers.py:139-146`).
`_ActivityPauseSignal` (§4.4) is itself an `Exception` subclass. If the pause is reached on this
activity's *final* `SAFE_RETRY` attempt (a genuinely reachable state — a couple of earlier attempts
failed transiently, then the next attempt hits a clarification question) and the signal is left to
propagate *out of* `work()` before being caught — e.g., caught only at the outer activity-function
level, around the `_guarded(...)` call itself — `_guarded`'s own `except Exception` intercepts it
first: `is_final_attempt` is true, so it calls `fail_job` (marking the Planning job **FAILED**,
client-visibly terminal) before re-raising. An outer catch would still convert the re-raised signal
into a normal `{"outcome": "paused", ...}` return, so the workflow proceeds as if paused — but the
job record has already been marked FAILED as a side effect, producing a client-visible
contradiction (terminal-FAILED job, workflow still actively running a pause/resume cycle).
**Contract requirement:** the `_ActivityPauseSignal` catch (or PRA's own `answer_callback`
mechanism reaching the same effect) must sit *inside* the callable passed as `work` to `_guarded` —
converting it to the `{"outcome": "paused", ...}` value there, as a normal return, so it never
becomes an `Exception` `_guarded` itself observes. This is a placement requirement on the
implementation, not a behavior change to `_guarded` itself; `_guarded` needs no modification.

### 4.3.2 Rollout compatibility for the activity signature change

`document_production_activity` is currently called with three positional args —
`args=[job_id, repo_path, ...]`-style, matching every other per-phase activity in this workflow
(`temporal/workflows.py:142-215`, e.g. `intake_activity` at :142-146) — not the single `request`
dict this contract's retry/continuation loop (§4.3) needs in order to carry
`acknowledged_resume_token`. Changing the activity's calling signature is itself a workflow-history
compatibility hazard, independent of the pause feature: a `PlanningWorkflow` execution whose
history was recorded *before* the signature change (i.e., it already scheduled
`document_production_activity` with the old three-positional-arg shape) must replay
deterministically against a worker that has since deployed the new dict-based call — Temporal
requires the *same sequence of commands* on replay, and a changed argument shape for the same
activity name is exactly the kind of non-deterministic edit `workflow.patched` exists to guard.

**Contract requirement:** gate the *entire new command sequence* — not just the argument shape —
behind `workflow.patched(...)`, the same mechanism `PlanningWorkflow` already uses for its own
prior migration (`_PER_PHASE_PATCH`, `temporal/workflows.py:122-140` — "A `PlanningWorkflow`
execution started before the per-phase migration... replays the legacy single-activity path via
the `workflow.patched` gate"). This must cover more than the argument shape: this contract also
*inserts a brand-new command* — the `document_production_pra_submit_activity` call (§4.3.1) —
*before* the existing `document_production_activity` call. Temporal replay requires the exact same
sequence of commands a history originally recorded; a history that scheduled
`planning_document_production` directly (no submit-activity command before it) diverges from a
replay that now schedules a new activity command first, **even if `document_production_activity`
itself is called with its old argument shape** on that branch. So the single new patch marker (e.g.
`_CLARIFICATION_PAUSE_PATCH`) must gate the *whole* new sequence: on `not workflow.patched(...)`,
skip `document_production_pra_submit_activity` entirely and call `document_production_activity`
exactly as today (old args, no resume loop); on the patched branch, schedule the submit activity
first, then run the new `request`-dict call and retry/continuation loop of §4.3. This exactly
mirrors `_PER_PHASE_PATCH`'s own legacy branch, which replays its entire old single-activity
sequence rather than cherry-picking pieces of the new one — reused a second time in the same
workflow, not invented anew.

**`workflow.patched` alone is not sufficient — the activity worker needs its own compatibility
path.** `workflow.patched` only governs what a *workflow* schedules on replay; it says nothing
about the activity *worker* process. During a rolling deploy, an activity task already enqueued
under the old three-positional-arg shape (scheduled by an old-code workflow execution before the
new worker version rolled out) can be picked up by a worker that has already registered the new
dict-only `document_production_activity` implementation — invocation fails before any workflow
code (patched or not) gets a chance to run, and under the activity's current `NO_RETRY` (§4.3.1)
that failure fails the whole workflow. **Contract requirement:** the activity function itself must
accept both call shapes — either a compatibility decoder at the top of
`document_production_activity` that detects the old three-positional-arg invocation and normalizes
it into the same `request` dict the rest of the function expects, or registering the new dict-based
behavior under a distinct `@activity.defn(name=...)` (e.g. `planning_document_production_v2`) while
the old name/signature stays registered and callable until the task queue has drained of old-shape
tasks. `workflow.patched` and this activity-level decoding are both required, addressing two
different compatibility surfaces (workflow replay vs. activity worker invocation) — neither
substitutes for the other.

### 4.4 Explicit hitl.py / pause_cycle.py reuse statement

This design reuses, unmodified in behavior:
- The `_ActivityPauseSignal`-style unwind: an internal control-flow signal raised deep inside the
  document-production call path, caught **inside the `_guarded`-wrapped `work`/`_work` callable
  itself** — not merely "at the activity function's own boundary" as a loose description might
  suggest — and translated into the `{"outcome": "paused", ...}` return value there, before
  `_guarded` ever sees an exception (§4.3.1's `_guarded`-interaction requirement below spells out
  why this placement specifically is load-bearing).
- `mint_resume_token`'s exact format and one-mint-per-pause-round rule.
- `_check_pending_pause_reentry`'s three-way classification (no pause / consume / re-emit
  unchanged).
- The job-record pause envelope field names: `waiting_for_answers`, `resume_token`, `pause_kind`,
  `pause_context`, `pending_questions` — Planning's job store (`job_store.py:45-46`) already seeds
  `pending_questions: []` and `waiting_for_answers: False` on every record, so no new fields are
  needed, only new writers.
- The `submit_answers` signal name and payload shape (§4.1).
- The workflow-side wait/buffer state machine (§4.2), copied field-for-field.
- The persist-then-signal answer-submission pattern from
  `coding_team_hitl.submit_pending_answers` (`api/routes/coding_team_hitl.py:20-71`): append
  validated answers to the job record, then signal — never the reverse, never signal-only (§4.3).
- `workflow.patched` for rollout compatibility (§4.3.2) — `PlanningWorkflow`'s own existing
  `_PER_PHASE_PATCH` mechanism, applied a second time rather than inventing a new versioning
  approach.

The only Planning-specific pieces are:
1. **Which activity calls the pause cycle** — `document_production_activity`
   (`temporal/activities.py:285-377`) instead of the coding team's planning/execution activities.
2. **The question source feeding it** — `OpenQuestion` / `resolve_pra_answers`
   (`planning_team/orchestrator.py:45-81`, `planning_team/models.py:216-244`) instead of Tech Lead
   clarify / worker escalation. `OpenQuestion` → `PendingQuestion` conversion is a straightforward
   field mapping (both already share `id`/`question_text`/`context`/`options` shapes); this is
   implementation detail for #7445-B, not a contract decision.
3. **One new `pause_kind` value** (`"planning_clarification"`) rather than reusing one of the
   coding team's three — see §4.1's rationale.
4. **An added external-job checkpoint** (§4.3.1): the coding team's pause has no equivalent,
   because its pause boundary never crosses into a separate external job. Planning's does (PRA), so
   this contract adds `save_checkpoint`/`load_checkpoint` (`shared/temporal/checkpoints.py`) around
   the PRA job id specifically to prevent resubmission on resume. This is a genuine addition to the
   coding-team pattern, not a divergence from it — it uses machinery the platform already documents
   as the sanctioned tool for exactly this ("phase boundaries inside an activity so a retried
   workflow can skip completed phases").
5. **Round-scoped answer persistence** (§4.3): the coding team's Tech-Lead clarify loop is
   effectively single-round per pause; Planning's PRA integration can raise multiple question
   rounds within one activity run, so the persisted `submitted_answers` must be scoped per
   `resume_token` rather than accumulated into one undifferentiated list — otherwise a later
   round's callback can resubmit an earlier round's already-consumed answers.
6. **A retry-policy split, not a blanket correction** (§4.3.1): `document_production_activity`
   currently runs entirely under `NO_RETRY`. This contract splits its PRA-facing work into two
   Temporal activities — a new `document_production_pra_submit_activity` that stays `NO_RETRY`
   (submission has no idempotency key to make it safely retryable) and
   `document_production_activity` itself, which drops `NO_RETRY` in favor of `SAFE_RETRY`
   specifically (not the coding team's unbounded default — `_guarded`'s finite-`max_attempts`
   contract requires a bounded policy, §4.3.1) because it never submits, only
   polls/pauses/resumes against an already-checkpointed job. Planning's current
   blanket `NO_RETRY` is the outlier; a single blanket policy is not the fix, a split one is.

No divergent mechanism is introduced anywhere in this design.

---

## 5. Contract: Preconditions / Postconditions / Invariants

The primitive #7445-B builds must satisfy:

**`document_production_pra_submit_activity` (§4.3.1 — separately scheduled, `NO_RETRY`)**
- *Preconditions:* Called with the Planning `job_id` **and** the post-synthesis `context`
  (specifically `repo_path` and the synthesized spec content) — the same inputs
  `document_production_activity` receives today (`temporal/activities.py:286-289`). `run_pra`
  requires `repo_path`/`spec_content` (`adapters/product_analysis.py:33-48`); at the point this
  activity runs, that content exists only in the workflow's in-memory `context` threaded from the
  synthesis phase (`temporal/workflows.py:176-181`), not yet durably written anywhere the submit
  activity could otherwise read it from — the job record at this point carries `repo_path` but not
  the synthesized spec (`DocumentProductionAgent.run` is what writes `initial_spec.md`, and it
  hasn't run yet). The workflow must pass `context` (or the specific `repo_path`/`spec_content`
  fields) into this activity's call, not just `job_id`. The `"planning_team"`-namespaced job record
  is readable; scheduled by the workflow with `retry_policy=NO_RETRY`.
- *Postconditions:* Checks `load_checkpoint("planning_team", job_id, "document_production_pra")`
  first. If present, returns immediately (no-op — a prior successful run already submitted). If
  absent, calls `run_pra(...)`. **`run_pra`/`run_product_analysis` returns `None` when the
  Software Engineering service is unconfigured or the submission POST fails
  (`adapters/product_analysis.py:33-48`) — this activity must treat a `None`/falsy return as a
  failure and raise, not persist a checkpoint with `pra_job_id: None` and return successfully.** A
  raised exception here fails this `NO_RETRY` activity (and, per the workflow's own error handling,
  the workflow) loudly and visibly, which is the correct outcome: `document_production_activity`'s
  precondition requires a checkpoint carrying a *usable* `pra_job_id` before it will ever call
  `wait_pra`, and there is no defined no-PRA fallback for a job that requested
  `use_product_analysis=True` but got no PRA job — that is an operational failure to surface, not
  paper over. Only on a genuine successful `pra_job_id`, immediately persist
  `save_checkpoint("planning_team", job_id, "document_production_pra", {"pra_job_id": ...})` as its
  own atomic write, then return.
- *Invariants:* `run_pra` is called at most once per Planning `job_id`, ever, *when this activity
  itself does not crash between the two steps*; a crash in that narrow window is the one residual
  risk this spec cannot close without PRA-side idempotency (§5's open risks, below) — `NO_RETRY`
  ensures that crash fails the workflow loudly rather than Temporal silently retrying into a
  duplicate submission. A checkpoint is never persisted with a `None`/falsy `pra_job_id`.

**`document_production_activity` (entry — every invocation, paused or not)**
- *Preconditions:* **On the patched branch** (`workflow.patched(_CLARIFICATION_PAUSE_PATCH)` is
  `True`, §4.3.2), called with a `request` dict optionally carrying `acknowledged_resume_token`.
  **On the legacy branch** (`not workflow.patched(...)` — a `PlanningWorkflow` execution whose
  history predates this feature), called with the original three positional args (`job_id`,
  `repo_path`, ... — `temporal/workflows.py:189-191`'s current shape), never a `request` dict, and
  never with `acknowledged_resume_token` (the legacy branch skips
  `document_production_pra_submit_activity` and the retry/continuation loop entirely — §4.3.2 — so
  this precondition holds trivially there: there is no pause to resume from). The activity
  implementation must decode/normalize both call shapes into one internal `request`-dict-equivalent
  before proceeding, per §4.3.2's activity-level compatibility requirement. In all cases: the
  `"planning_team"`-namespaced job record for the job is readable; **when
  `request["use_product_analysis"]` is `True`** (patched branch only — the legacy branch's
  `use_product_analysis` positional arg governs identically, just without this contract's pause
  machinery),
  `load_checkpoint(...)` for `"document_production_pra"` already returns a checkpoint (the workflow
  only enters this activity after `document_production_pra_submit_activity` above has completed) —
  when `False`, no checkpoint precondition applies, since PRA (and this whole pause contract) never
  engages; runs under `SAFE_RETRY` specifically (§4.3.1 — not `NO_RETRY`, and not the SDK's
  unbounded default, which is incompatible with the `_guarded` wrapper's finite-`max_attempts`
  contract).
- *Postconditions:* Loads the checkpoint and calls `wait_pra(job_id=<checkpointed pra_job_id>,
  ...)` directly. This activity **never** calls `run_pra` itself, under any circumstance.
- *Invariants:* Every entry (fresh call, Temporal retry, or workflow-driven resume) reuses the
  checkpointed `pra_job_id` unconditionally; retrying this activity can never trigger a second PRA
  submission, because the only code path that submits lives in the other, `NO_RETRY` activity.

**`document_production_activity` (paused-return path)**
- *Preconditions:* PRA reports unanswered `OpenQuestion`s and no matching persisted pause is being
  resumed (per `_check_pending_pause_reentry`, §4.3).
- *Postconditions:* The activity persists `{waiting_for_answers: True, resume_token, pause_kind:
  "planning_clarification", pause_context: None, pending_questions}` to the job record as its own
  atomic `update_job` call (separate from, and after, the checkpoint write above — no longer
  required to be combined with it, since checkpoint-presence alone already prevents resubmission),
  then returns (does not raise) `{"outcome": "paused", "job_id", "resume_token", "pause_kind",
  "pause_context", "pending_questions"}` — no further job-store read or blocking call past that
  point.
- *Invariants:* The activity never blocks waiting for a human answer. It is safe to call multiple
  times for the same `job_id`/pause round: a call that finds a persisted pause whose token does not
  match `acknowledged_resume_token` re-emits the same paused payload unchanged, performing no new
  PRA work and no duplicate persistence.

**`document_production_activity` (resume path)**
- *Preconditions:* `request["acknowledged_resume_token"]` equals the job record's persisted
  `resume_token`; the job record's answer store (persisted by the answer-submission path *before*
  it signaled — §4.3) carries a batch tagged with that same `resume_token` covering every question
  in `pending_questions`; a PRA-job-id checkpoint for this job is present (§4.3.1).
- *Postconditions, in this order (§4.3's ordering requirement below):* (1) the answer batch tagged
  with the resumed `resume_token` — never the full accumulated answer history, never the signal
  payload directly — is submitted to PRA via `submit_product_analysis_answers`/`answer_callback`;
  (2) only once that submission is confirmed applied does the activity atomically clear the pause
  envelope (`waiting_for_answers`, `resume_token`, `pause_kind`, `pause_context`,
  `pending_questions`) *and* mark that batch consumed (or move it into `resolved_questions`) in one
  job-record update; (3) `wait_pra` resumes polling against the checkpointed external PRA job id —
  `run_pra` is not called again; the activity proceeds to its normal terminal return shape (or
  pauses again, for the next round, per the paused-return path above).
- *Invariants:* A resume is applied at most once per `resume_token` — re-invocation with the same
  already-consumed token must not re-apply answers or re-run already-completed work (idempotent
  resume). No resume path may submit a second external PRA job for the same Planning job. A
  resumed round's answer_callback never returns an answer belonging to a different `resume_token`.
  Because this activity is retryable (§4.3.1), a retry that lands *after* step (1) but before step
  (2) must not resubmit the answers blindly — it must first reconcile against PRA's current
  `pending_questions` (a status GET). **This reconciliation must compare full question identity
  (`id` *and* `question_text` together), never `id` alone.** PRA's own question parser falls back
  to a positional `q{index}` id when a question's own `id` is missing
  (`product_requirements_analysis_agent/question_processing.py:773,805,821`), and PRA's answers
  route validates only `id` membership in the *current* `pending_questions`
  (`api/routes/product_analysis.py:262-275`) — it has no per-round or version identifier. So a
  retry that checks only "is `q0` still pending" cannot tell "PRA already applied my answer and
  moved on" apart from "PRA advanced to an unrelated later round that happens to reuse `q0` for a
  different question" — reconciling by id alone risks silently applying round *N*'s stale answer
  content to round *N+1*'s differently-scoped question, which PRA's id-only validation would
  accept without complaint. Comparing the *full* `(id, question_text)` pair against what this pause
  round persisted removes the ambiguity: a match means "PRA is still waiting on exactly this
  question, safe to (re-)submit"; no match (the id is gone, or present with different
  `question_text`) means "PRA has moved past this round already — do not submit stale content,"
  proceeding straight to step (2) instead. This is the same class of already-flagged limitation as
  the PRA-idempotency open risk (§5, below): Planning's contract can defend against confusion here,
  but a durable PRA-side round/version identifier would close the gap more completely than any
  reconciliation Planning does on its own — out of `planning_team`'s boundary, as before.

**A failed status read is not confirmation.** `get_product_analysis_status` returns `None` on
*any* GET failure (`adapters/product_analysis.py:51-58` — "Returns `None` on failure"), not only
when the job or question is genuinely gone. Treating "no match" (§5's reconciliation rule above) as
proof PRA already applied the answer would misinterpret a transient status-read failure — after an
ambiguous prior POST — as confirmation, clearing the local pause envelope and consuming the durable
answer batch while PRA may still be sitting there waiting for it, with nothing left locally to
retry from. **Contract requirement:** reconciliation must distinguish "the status call itself
failed" from "the status call succeeded and the question is structurally absent/mismatched." Only
the latter is treated as confirmation (proceed to step (2)); a `None`/failed status response, or
any other structurally invalid response, must cause the activity to raise (so `SAFE_RETRY`
re-attempts the whole reconciliation later) rather than proceed as if confirmed.

**Ordering requirement this adds to §4.3:** clearing the local pause envelope / marking a batch
consumed must never happen *before* PRA has durably applied that batch. Because `submit_answers`
(§4.3) already establishes that the job record's answer batch — not the workflow's in-memory
`_submitted_answers` — is the durable source of truth, and because a worker crash between "PRA
applied the answer" and "the pause envelope is cleared" is recoverable (retry re-checks PRA per the
reconciliation invariant above and finds it already applied), the reverse ordering is not: clearing
the envelope first and then crashing before PRA sees the answer would leave PRA still waiting with
no persisted pause to resume from, silently hanging the job.

**`PlanningWorkflow.submit_answers` (signal handler)**
- *Preconditions:* None on the caller — a signal handler must accept any payload without raising
  (Temporal signal handlers cannot reject a signal back to the sender).
- *Postconditions:* A payload whose `resume_token` matches `self._active_resume_token` and is the
  first such match sets `self._submitted_answers`; every other payload shape (no active pause, or
  mismatched, or duplicate token) is either buffered (no active pause yet) or silently ignored
  (mismatched or duplicate), never raises, never sets `_submitted_answers` a second time for one
  round.
- *Invariants:* `self._buffered_signals` holds at most one entry per distinct `resume_token` seen
  while no pause was active; every entry is discarded the moment a new pause is armed and its
  matching entry (if any) is applied — the dict cannot grow unbounded across a long-running
  workflow's many pause rounds.

**Open risks, not resolved by this spec:**
1. *Carried over from `hitl_pause_resume_contract.md` §4:* `workflow.wait_condition` here is
   unbounded — no timeout and no reconciliation against job-record cancellation. #7445-B inherits
   this exact caveat from the coding-team implementation; it is not a new gap introduced by
   Planning reuse, and remains open future work for both teams alike.
2. *New to this spec, cross-team:* PRA submission (`run_pra`/`run_product_analysis`) has no
   idempotency key, so a worker crash in the narrow window between `run_pra` returning and the
   checkpoint write landing (§4.3.1) can still produce a duplicate external PRA job — mitigated
   here by keeping that narrow step `NO_RETRY`, but not eliminated. Full closure requires
   `software_engineering_team`'s `/product-analysis/run` endpoint to accept a client-supplied
   idempotency key or equivalent reconciliation lookup; that is outside `planning_team`'s boundary
   and this spec's stated scope, and is flagged here as a prerequisite for fully retryable PRA
   submission rather than something #7445-B can close alone.

---

## 6. References

- `system_design/specs/SPEC-023-coding-team-human-in-the-loop.md` — the direct precedent this spec
  extends to Planning.
- `backend/agents/software_engineering_team/system_design/hitl_pause_resume_contract.md` — the
  detailed contract doc for the coding-team primitive being reused.
- `backend/agents/software_engineering_team/hitl.py`, `pause_cycle.py`,
  `temporal/coding_team_workflow.py`, `api/routes/coding_team_hitl.py` — the implementation being
  mirrored, including the persist-then-signal answer-submission route (§4.3).
- `backend/shared/hitl/models.py` — the shared `PendingQuestion`/`AnswerSubmission`/
  `SubmitAnswersRequest` schemas this contract reuses.
- `backend/shared/temporal/README.md` / `checkpoints.py` — `save_checkpoint`/`load_checkpoint`
  (used in §4.3.1 for the PRA-job checkpoint) and the sanctioned thread-mode equivalent
  (`wait_for_input`/`submit_input`), out of scope here but the natural companion for non-Temporal
  callers.
- `backend/agents/planning_team/orchestrator.py`, `models.py`, `temporal/workflows.py`
  (retry policies at :53-74, `_PER_PHASE_PATCH` at :114-140, `document_production_activity` call
  at :189-197), `temporal/activities.py`, `api/main.py`, `job_store.py`,
  `agents/document_production/agent.py`, `phases/document_production.py`,
  `adapters/product_analysis.py` (`wait_for_product_analysis_completion`'s multi-round poll loop,
  §4.3) — the Planning-side files this contract will be implemented against in #7445-B/#7446.
- Integration tests demonstrating the exact signal/wait pattern end-to-end (worth mirroring for
  Planning's own test suite in #7445-B):
  `backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py`
  (`test_workflow_pauses_then_resumes_to_completion_via_signal`,
  `test_workflow_survives_worker_restart_while_paused_with_buffered_signal`,
  `test_workflow_resumes_via_early_signal_buffered_before_pause_processed`).
