# Planning HITL Temporal Contract (Answer-Callback Primitive)

This documents the Temporal-signal-based `answer_callback` primitive added in
`planning_team/temporal/answer_signal.py`. It follows the same
signal + `wait_condition` shape as the coding team's existing HITL primitive
(`software_engineering_team/temporal/coding_team_workflow.py`'s
`submit_answers` signal, documented in that team's own
`system_design/hitl_pause_resume_contract.md`), scoped to Planning's much
simpler callback contract.

> **Citation freshness:** file/line citations below are accurate as of the
> commit this document was written against. Resolve a citation that no
> longer matches by re-locating the named symbol, not by trusting the line
> numbers.

## Problem

Planning's `resolve_pra_answers` (`planning_team/orchestrator.py:45-80`)
expects an optional `answer_callback: Callable[[list], list]`. Thread mode
satisfies it with `_build_planning_answer_callback`
(`software_engineering_team/orchestrator.py:373-405`), which busy-polls the
job-service record from the calling **thread** — legal there, illegal inside
a Temporal activity or workflow sandbox. Today's Temporal path
(`planning_team/temporal/activities.py`'s `_pra_answer_cb`) passes no
callback at all and lets `resolve_pra_answers` auto-answer with defaults —
silently, with no human in the loop.

A plain Temporal *activity* cannot natively suspend for an arbitrary human
response time; only a *workflow* can `await workflow.wait_condition(...)`
durably (surviving worker restarts). This primitive is the reusable building
block that lets a callback presented to Planning's code look synchronous
while the actual wait happens at the workflow level.

**Scope of this primitive** (issue-tracked): the signal/wait mixin and the
`Callable[[list], list]` adapter, unit-tested in isolation. Wiring this into
`planning_team/temporal/activities.py`'s `document_production_activity` (so a
real workflow drives the pause loop end-to-end) is separate follow-on work —
this primitive is deliberately usable by, but not yet used by, any concrete
workflow class.

## Signal contract

- Signal name: `submit_planning_answers`.
- Payload (plain dict, not a Pydantic model — a signal handler must never
  raise, since an unhandled exception fails the workflow task and, because
  Temporal replays history, fails identically on every future replay):

  ```json
  {"resume_token": "<str>", "answers": [{"question_id": "...", "selected_option_id": "..."}]}
  ```

- Validation/match rules mirror `submit_answers` exactly: a malformed payload
  (not a dict, non-list `answers`, missing/empty `resume_token` while no
  pause is active) is dropped, not raised; a signal for a not-yet-armed pause
  is buffered by `resume_token` (first submission per token wins); a
  mismatched token while a pause *is* active is ignored; a second signal for
  an already-resolved token is ignored.

## Control flow

1. Planning code calls `answer_callback(questions)` (unchanged call site —
   Planning does not need to know it's running under Temporal).
2. The callback, built by `build_temporal_planning_answer_callback(resume_token,
   submitted_answers=None)`, has no answers yet, so it raises
   `PlanningAnswerPauseSignal(resume_token, pending_questions=questions)` — an
   activity-safe exception, never a blocking call. A future activity wrapper
   (the deferred wiring work) catches this and returns a discriminated
   `{"outcome": "paused", "resume_token": ..., "pending_questions": ...}`
   result instead of letting the activity hang, exactly like
   `_ActivityPauseSignal` does for the coding team today.
3. A workflow that mixes in `PlanningAnswerSignalMixin` sees that paused
   result and calls `await self.wait_for_planning_answers(resume_token)` —
   which arms the wait, drains any already-buffered signal for that token,
   and suspends on `workflow.wait_condition(lambda: self._submitted_answers
   is not None)`. No timeout: the predicate is satisfied only by a real,
   token-matched `submit_planning_answers` signal — the workflow never
   silently proceeds with a default answer.
4. Once the signal lands, the workflow re-invokes Planning's phase with a
   fresh callback built via `build_temporal_planning_answer_callback(resume_token,
   submitted_answers=<the resolved answers>)`, which this time simply
   filters and returns them (by `question_id`), matching the shape thread
   mode already produces.

## Why a mixin, not an inline copy per workflow

The coding team's implementation lives inline inside `CodingTeamWorkflow`
because that workflow class only ever needs this once. Planning's version is
built as a standalone `PlanningAnswerSignalMixin` any `@workflow.defn` class
can inherit, since the concrete workflow that will drive Planning's
document-production phase under Temporal does not exist yet (deferred
wiring work decides which workflow class owns it). The mixin keeps the
signal handler + wait/state-machine logic in one tested place rather than
duplicating `CodingTeamWorkflow`'s pattern by hand a second time.
