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
4. Once the signal lands, the workflow resumes with a fresh callback built via
   `build_temporal_planning_answer_callback(resume_token,
   submitted_answers=<the resolved answers>)`, which this time simply filters
   and returns them (by `question_id`), matching the shape thread mode
   already produces.

## Open problem for the deferred wiring work: re-invoking the phase is not enough

Naively "re-invoking Planning's phase" in step 4 above does **not** work for
`document_production_activity`'s PRA path, and the deferred wiring work
must not implement it that way. `DocumentProductionAgent.run`
(`planning_team/agents/document_production/agent.py`) calls
`run_pra(...)` to mint a PRA `job_id` first, and only *then* calls
`wait_pra(job_id=job_id, answer_callback=answer_callback)` — `answer_callback`
is invoked from inside `wait_pra`'s polling loop, after the PRA job already
exists. `PlanningAnswerPauseSignal` is raised from inside that callback, so
unwinding out of the activity loses the only handle to that job: its
`resume_token`/`pending_questions` payload carries no PRA `job_id`. Restarting
the whole phase on resume calls `run_pra` again, minting a **second** PRA job
(whose generated questions may not even share the first job's question IDs),
while the original PRA job is left paused/polling forever with no one left to
answer it.

The deferred wiring must instead preserve enough state across the pause to
resume against the **original** PRA job — e.g. include the PRA `job_id` in
the pause payload (requires splitting `run_pra` and `wait_pra` into separate
activities so the workflow can hold the `job_id` between them, rather than
calling both from inside one activity invocation of `document_production_activity`
as today) and re-enter `wait_pra` against that same `job_id` on resume, not
re-run `DocumentProductionAgent.run` from its start.

## Open problem for the deferred wiring work: PRA asks more than once

PRA's own review loop (`software_engineering_team/product_requirements_analysis_agent/agent.py`,
`while iteration < max_iterations`) can raise more than one distinct
clarification round before completing — each round has its own, unrelated
`pending_questions` batch. `wait_for_product_analysis_completion`
(`planning_team/adapters/product_analysis.py`'s `_on_poll`) calls
`answer_callback(pending)` again on every poll while `waiting_for_answers`
stays true, which includes a second (or third) round with brand-new question
IDs.

A single `submitted_answers=<...>` "resolved" callback built once in step 4
above cannot serve more than one round: called again with a later round's
(different) question IDs, `build_temporal_planning_answer_callback`'s filter
correctly finds no match and returns `[]` — by design, it never fabricates an
answer for an unmatched question. But `_on_poll` treats an empty return as
"nothing to submit yet" (`if answers: submit_product_analysis_answers(...)`)
and just keeps polling, so the workflow never re-pauses for that second
round; PRA's own `total_timeout` eventually expires the whole wait silently
instead. The deferred wiring must therefore detect, on each `answer_callback`
invocation, whether the current `questions` batch is the one already resolved
or a **new** one — and for a new batch, raise a fresh
`PlanningAnswerPauseSignal` with a new `resume_token` (re-arming
`wait_for_planning_answers`) rather than reusing the one-shot resolved
callback for the rest of the phase.

## Open problem for the deferred wiring work: a rejected submission looks the same as success

`build_temporal_planning_answer_callback`'s resolved callback validates only
that `question_id` is a `str` that matches a pending question — it does not
(and, being generic/PRA-agnostic, should not) validate `selected_option_id`
or `other_text` against whatever the eventual submission endpoint expects. A
malformed `submit_planning_answers` signal (e.g. `selected_option_id: []`)
therefore passes through unchanged, and PRA's actual submission endpoint
(`SubmitAnswersRequest` at `POST .../product-analysis/{job_id}/answers`,
called via `submit_product_analysis_answers`) will reject it with a 422 —
which `post_json` (`shared/http/job_polling.py`) turns into a plain `None`
return, indistinguishable from any other transient failure.

`_on_poll`'s `if answers: submit_product_analysis_answers(...)` only checks
that the callback returned a non-empty list, not that the submission it
triggered actually succeeded — so a rejected, malformed answer batch is
silently dropped and PRA just keeps polling until its own `total_timeout`,
exactly the same silent-hang failure mode as the multi-round gap above. The
deferred wiring must check `submit_product_analysis_answers`'s return value
and treat a `None` (rejected/failed submission) as still-unanswered — re-
raising a pause for the same batch — rather than treating "the callback
returned something" as proof the human's answer was actually accepted.

## Why a mixin, not an inline copy per workflow

The coding team's implementation lives inline inside `CodingTeamWorkflow`
because that workflow class only ever needs this once. Planning's version is
built as a standalone `PlanningAnswerSignalMixin` any `@workflow.defn` class
can inherit, since the concrete workflow that will drive Planning's
document-production phase under Temporal does not exist yet (deferred
wiring work decides which workflow class owns it). The mixin keeps the
signal handler + wait/state-machine logic in one tested place rather than
duplicating `CodingTeamWorkflow`'s pattern by hand a second time.
