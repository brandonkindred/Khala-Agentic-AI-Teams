# shared.hitl

Single owner of the **human-in-the-loop (HITL)** contract: the pending-question /
answer Pydantic schemas, the shared `HumanReview` gate model (approve/reject +
optional feedback), answer-submission validation, progress coercion, and status
materialization that teams reuse when a job pauses to ask the user a product/design
decision or to collect a human gate decision.

Previously each team defined its own near-identical copy and the copies had drifted
into genuinely different behavior. This package reconciles them to the **strictest /
superset** behavior, and each team is repointed via *extract-then-shim* (old models
re-export these; the old helpers become thin wrappers).

## Public API

| Symbol | Module | Purpose |
|---|---|---|
| `QuestionOption`, `PendingQuestion`, `AnswerSubmission`, `SubmitAnswersRequest` | `models` | The four schemas, superset fields folded in as `Optional`/defaulted. |
| `HumanReview` | `models` | Gate decision: required `approved`, optional `feedback` (default `""`). |
| `validate_answers(data, request)` | `validation` | Validate a submission against a job's pending questions; raise `HTTPException` (400, or 500 on a corrupted record) or return answer dicts. |
| `coerce_progress(value)` | `progress` | Coerce a stored progress value to an int clamped to `[0, 100]`, or `None`. |
| `pending_questions_from_raw(raw)` | `status` | Materialize stored records into `PendingQuestion` models with `model_validate` (full-fidelity; skips non-dict entries). |

## Reconciled contract

`validate_answers` rule order (the 500-vs-400 distinction is deliberate):
not-waiting → 400 · no-pending → 400 · a pending question missing `id` → **500**
(corrupted server record) · duplicate `question_id` in the request → 400 ·
missing required → 400 · unknown question id → 400 · per-answer
(`other` without non-blank text → 400 · a non-`other` option the question never
offered → 400 · neither an option nor text → 400). Returned answer dicts carry
`question_text` so a resume can re-match answers to re-asked questions by text.

## Behavior changes adopted by software_engineering_team

Repointing SE onto this package raises it to coding_team's stricter behavior, which
changes three previously-observable SE behaviors (each covered by a regression test):

1. **Progress clamps to `[0, 100]`** — SE previously passed stored progress through
   unbounded, so a corrupt record could render an out-of-range bar.
2. **Answer validation gains the corrupted-record (500) and duplicate-answer (400)
   rejections** — SE previously accepted two conflicting answers to one question.
3. **`get_job_status` preserves `recommendation`/`allow_multiple`** — SE's route
   previously hand-enumerated `PendingQuestion` fields and silently dropped these two.

## Non-shared: team WorkflowStatus

Branding (`READY_FOR_ROLLOUT`) and market research (`DRAFT` / `READY_FOR_EXECUTION`)
keep local `WorkflowStatus` enums because terminal semantics differ per team.
Investment's `WorkflowStatusResponse` is an unrelated API DTO and is not part of
this package.

## Layout & conventions

Bare package under `backend/shared/` (no `pyproject.toml`); resolved on `sys.path`
via `pythonpath = agents .` in `backend/pytest.ini`. Design-by-Contract docstrings
throughout. `tests/` runs locally via `make test` and stands as the standalone
"prove the reconciled behavior" suite; in CI the moved code is coverage-gated at 90%
through the `combine-shared-infra` job (SE + coding_team suites exercise it).
