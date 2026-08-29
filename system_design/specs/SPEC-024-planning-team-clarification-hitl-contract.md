# SPEC-024: Planning Team Clarification-Question Temporal Signal/Wait Contract

| Field        | Value                                                                 |
|--------------|-----------------------------------------------------------------------|
| **Status**   | Proposed (design only — no production code in this story)             |
| **Author**   | Platform Engineering                                                  |
| **Created**  | 2026-08-29                                                            |
| **Priority** | P0 (blocks #7445-B / #7445-C)                                         |
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
- UI/REST surface changes beyond noting that the existing `SubmitAnswersRequest.resume_token`
  field already accommodates this contract.

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
`{"question_id": str, "selected_option_id": Optional[str], "other_text": Optional[str]}`.

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
- `acknowledged_resume_token` matches the persisted token → genuine resume; clear the pause
  envelope, apply the now-answered questions via `resolve_pra_answers(..., answer_callback=<from
  job record>)`, and continue past the point PRA raised them.
- Token missing/mismatched but a pause is persisted → this is a pre-work activity retry (e.g.
  Temporal retried the activity after it persisted-but-not-yet-returned the pause); re-emit the
  exact same `{"outcome": "paused", ...}` payload unchanged, doing no new PRA work.

**Recommended extraction (decision for #7445-B, not mandated here):** `mint_resume_token` and
`_check_pending_pause_reentry` have no coding-team-specific logic — they operate purely on a job
record dict and a resume token. #7445-B should consider extracting them from
`software_engineering_team/pause_cycle.py` into a new `backend/shared/hitl/pause_cycle.py`
(alongside the existing `shared/hitl/models.py`), so Planning **imports** the primitive rather than
duplicating it or reaching across team boundaries into `software_engineering_team`. This spec does
not mandate the extraction's exact shape — only that Planning's implementation must not diverge in
behavior from what's documented in §4.4 below, however the code ends up organized.

### 4.4 Explicit hitl.py / pause_cycle.py reuse statement

This design reuses, unmodified in behavior:
- The `_ActivityPauseSignal`-style unwind: an internal control-flow signal raised deep inside the
  document-production call path, caught at the activity function's own boundary and translated
  into the `{"outcome": "paused", ...}` return value — never propagated further.
- `mint_resume_token`'s exact format and one-mint-per-pause-round rule.
- `_check_pending_pause_reentry`'s three-way classification (no pause / consume / re-emit
  unchanged).
- The job-record pause envelope field names: `waiting_for_answers`, `resume_token`, `pause_kind`,
  `pause_context`, `pending_questions` — Planning's job store (`job_store.py:45-46`) already seeds
  `pending_questions: []` and `waiting_for_answers: False` on every record, so no new fields are
  needed, only new writers.
- The `submit_answers` signal name and payload shape (§4.1).
- The workflow-side wait/buffer state machine (§4.2), copied field-for-field.

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

No divergent mechanism is introduced anywhere in this design.

---

## 5. Contract: Preconditions / Postconditions / Invariants

The primitive #7445-B builds must satisfy:

**`document_production_activity` (paused-return path)**
- *Preconditions:* Called with a `request` dict optionally carrying `acknowledged_resume_token`;
  the job record for `request["job_id"]` is readable.
- *Postconditions:* If PRA raises unanswered `OpenQuestion`s and no matching persisted pause is
  being resumed, the activity persists `{waiting_for_answers: True, resume_token, pause_kind:
  "planning_clarification", pause_context: None, pending_questions}` to the job record in one
  atomic update, then returns (does not raise) `{"outcome": "paused", "job_id", "resume_token",
  "pause_kind", "pause_context", "pending_questions"}` — no further job-store read or blocking call
  past that point.
- *Invariants:* The activity never blocks waiting for a human answer. It is safe to call multiple
  times for the same `job_id`/pause round: a call that finds a persisted pause whose token does not
  match `acknowledged_resume_token` re-emits the same paused payload unchanged, performing no new
  PRA work and no duplicate persistence.

**`document_production_activity` (resume path)**
- *Preconditions:* `request["acknowledged_resume_token"]` equals the job record's persisted
  `resume_token`, and the job record's `pending_questions` all have corresponding entries in the
  answers the workflow previously accepted via `submit_answers`.
- *Postconditions:* The pause envelope (`waiting_for_answers`, `resume_token`, `pause_kind`,
  `pause_context`, `pending_questions`) is atomically cleared from the job record; PRA continues
  past the clarification point using the now-resolved answers; the activity proceeds to its normal
  terminal return shape.
- *Invariants:* A resume is applied at most once per `resume_token` — re-invocation with the same
  already-consumed token must not re-apply answers or re-run already-completed work (idempotent
  resume).

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

**Open risk, carried over from `hitl_pause_resume_contract.md` §4 (not resolved by this spec):**
`workflow.wait_condition` here is unbounded — no timeout and no reconciliation against job-record
cancellation. #7445-B inherits this exact caveat from the coding-team implementation; it is not a
new gap introduced by Planning reuse, and remains open future work for both teams alike.

---

## 6. References

- `system_design/specs/SPEC-023-coding-team-human-in-the-loop.md` — the direct precedent this spec
  extends to Planning.
- `backend/agents/software_engineering_team/system_design/hitl_pause_resume_contract.md` — the
  detailed contract doc for the coding-team primitive being reused.
- `backend/agents/software_engineering_team/hitl.py`, `pause_cycle.py`,
  `temporal/coding_team_workflow.py` — the implementation being mirrored.
- `backend/shared/hitl/models.py` — the shared `PendingQuestion`/`AnswerSubmission`/
  `SubmitAnswersRequest` schemas this contract reuses.
- `backend/shared/temporal/README.md` (`checkpoints.py`) — sanctioned thread-mode equivalent
  (`wait_for_input`/`submit_input`), out of scope here but the natural companion for non-Temporal
  callers.
- `backend/agents/planning_team/orchestrator.py`, `models.py`, `temporal/workflows.py`,
  `temporal/activities.py`, `api/main.py`, `job_store.py` — the Planning-side files this contract
  will be implemented against in #7445-B/#7446.
- Integration tests demonstrating the exact signal/wait pattern end-to-end (worth mirroring for
  Planning's own test suite in #7445-B):
  `backend/agents/software_engineering_team/tests/test_coding_team_temporal_workflow.py`
  (`test_workflow_pauses_then_resumes_to_completion_via_signal`,
  `test_workflow_survives_worker_restart_while_paused_with_buffered_signal`,
  `test_workflow_resumes_via_early_signal_buffered_before_pause_processed`).
