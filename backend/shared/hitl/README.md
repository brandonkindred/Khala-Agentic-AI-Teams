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

`branding_team` and `market_research_team` each define their own run-lifecycle
`WorkflowStatus(str, Enum)`. Both were evaluated for consolidation into this
package alongside `HumanReview` and deliberately kept team-local — recorded here
as the decision, with the evidence behind it, rather than re-litigated per team.

**branding_team.WorkflowStatus** (`branding_team/models.py:43-52`): `NEEDS_HUMAN_DECISION`,
`READY_FOR_ROLLOUT`. Produced by the single pure function
`BrandingTeamOrchestrator._build_status_summary` (`orchestrator.py:443-475`), shared by
both the thread-mode run path and the Temporal `finalize_branding_activity` via
`_assemble_team_output`. Recomputed fresh into a new `TeamOutput` on every run — never
mutated in place.

| `human_review.approved` | `current_phase` | Result |
|---|---|---|
| `False` | any | `NEEDS_HUMAN_DECISION` |
| `True` | `< COMPLETE` | `NEEDS_HUMAN_DECISION` |
| `True` | `== COMPLETE` | `READY_FOR_ROLLOUT` (terminal) |

(`branding_team` also has three unrelated status vocabularies in the same package —
`BrandStatus` for the brand entity, job-store `JOB_STATUS_*` for background jobs, and ad
hoc session/question strings — none of which are part of this comparison.)

**market_research_team.WorkflowStatus** (`market_research_team/models.py:20-30`):
`DRAFT`, `NEEDS_HUMAN_DECISION`, `READY_FOR_EXECUTION`. Produced by the single pure
function `MarketResearchOrchestrator.assemble` (`orchestrator.py:231-291`), branching
only on `human_review.approved` (`True` → `READY_FOR_EXECUTION`, `False` →
`NEEDS_HUMAN_DECISION`). `DRAFT` is defined but currently dead — no code path produces it.

**investment_team**: no `WorkflowStatus` enum exists. `api/main.py`'s
`WorkflowStatusResponse` (`mode`, `audit_log`, `queue_counts`) is an unrelated API DTO
backed by `WorkflowMode` (`models.py:116-120`: `ADVISORY`/`PAPER`/`LIVE`/`MONITOR_ONLY`) —
a trading-mode setting, not a run-lifecycle status. Its other status vocabularies
(`AdvisorSessionStatus`, `PaperTradingStatus`, `ValidationStatus`, `JOB_STATUS_*`,
`STRATEGY_LAB_TERMINAL_STATUSES`) serve job/backtest/paper-trading lifecycles unrelated
to human approval.

The one investment_team enum that *is* approval-derived, and so was explicitly evaluated
against `WorkflowStatus`, is **`PromotionStage`** (`models.py:103-107`: `REJECT` /
`REVISE` / `PAPER` / `LIVE`), returned by `PromotionGateAgent.decide`
(`agents.py:136-300`). Unlike `WorkflowStatus`, its outcome isn't a single
`human_review.approved` branch — `decide` walks five independent, ordered gates
(separation-of-duties, risk veto, validation checklist, IPS live-trading permission,
then `ips.human_approval_required_for_live and not human_live_approval`), any of which
can short-circuit to `REJECT`/`REVISE`/`PAPER` before human approval is even checked;
only the last gate reaching `LIVE` depends on approval. Each decision also carries a
`gate_results: List[GateCheckResult]` trace and an `AuditContext` (`models.py:1124-1138`)
that `WorkflowStatus` has no equivalent of. Given the different shape (five-gate
checklist producing an audited decision, vs. a single boolean gate producing a display
tag) and the complete non-overlap in member names, `PromotionStage` was evaluated and
kept out of this comparison rather than silently omitted.

**Decision: keep independent (no shared enum).** Only the string `"needs_human_decision"`
is genuinely common between branding and market research; each team's terminal state
names a different domain outcome (`ready_for_rollout` vs. `ready_for_execution`), and
market research carries an extra, currently-unused `DRAFT`. A shared enum would need
either an awkward union of all per-team terminal values (defeating the point of an enum
as a closed set) or a lossy shared subset still requiring a per-team extension — more
complexity than the current two ~10-line enums. `investment_team`'s only
approval-adjacent enum, `PromotionStage`, was evaluated above and excluded on shape
(multi-gate audited checklist, not a boolean-derived tag) and naming, not overlooked.
The one piece that *was* genuinely duplicated across teams — the
`HumanReview` approve/reject gate — is already shared here; each team derives its own
`WorkflowStatus` from `human_review.approved` at the orchestrator boundary, which is
exactly why `WorkflowStatus` itself was not folded into this package.

## Layout & conventions

Bare package under `backend/shared/` (no `pyproject.toml`); resolved on `sys.path`
via `pythonpath = agents .` in `backend/pytest.ini`. Design-by-Contract docstrings
throughout. `tests/` runs locally via `make test` and stands as the standalone
"prove the reconciled behavior" suite; in CI the moved code is coverage-gated at 90%
through the `combine-shared-infra` job (SE + coding_team suites exercise it).
