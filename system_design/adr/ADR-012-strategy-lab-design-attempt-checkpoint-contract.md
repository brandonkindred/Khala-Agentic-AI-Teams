# ADR-012 — Intra-attempt checkpoint contract for `run_design_attempt_activity`

- **Status**: Proposed — design-note only, no behavior change. Foundation for the sibling
  implementation sub-issue under the parent epic.
- **Date**: 2026-08-12
- **Owner**: Investment Team / Strategy Lab
- **Related**:
  - Epic: "Strategy Lab: add intra-attempt checkpointing to the Temporal design-attempt
    activity" — this ADR resolves that epic's first acceptance criterion (a documented
    checkpoint contract, reconciled with `RETRY_STATE_ISOLATION.md`). The checkpoint-persistence
    activity itself, and wiring `run_design_attempt_activity` to check for and resume from it,
    are separate sibling sub-issues this ADR is written to unblock.
  - `backend/agents/investment_team/strategy_lab/temporal/activities.py:555-808` —
    `run_design_attempt_activity`, the activity this contract extends.
  - `backend/agents/investment_team/strategy_lab/orchestrator_design.py:1193` —
    `_run_design_attempt`, whose Phase 1 / Phase 1b boundary is the checkpoint's location.
  - `backend/agents/investment_team/strategy_lab/temporal/workflows.py:1-160` —
    `_ACTIVITY_RETRY`, `_DESIGN_ATTEMPT_TIMEOUT`, and the call site this contract's resume path
    is triggered from.
  - `backend/agents/investment_team/strategy_lab/RETRY_STATE_ISOLATION.md` — the existing
    copy-on-entry/commit-on-completion contract this checkpoint must not violate; amended
    alongside this ADR to cross-reference it.
  - `backend/shared/fencing.py` (`check_fencing_token`) and
    `backend/agents/investment_team/strategy_lab/run_state.py`
    (`get_run_generation_strict`) — the fencing primitives this contract reuses rather than
    inventing new ones.

## Context

`run_design_attempt_activity` wraps `StrategyLabOrchestrator._run_design_attempt` — the entire
design → synthesis → refinement/alignment → verification/analysis → record-assembly sequence for
one design attempt — as a single, opaque Temporal activity. One attempt can run up to
`_DESIGN_ATTEMPT_TIMEOUT` (2 hours) and make up to `STRATEGY_LAB_DESIGN_MAX_LLM_CALLS`-bounded LLM
round-trips (540 at the documented defaults, spanning all `MAX_DESIGN_REENTRIES` re-entries). The
activity is deliberately run whole and unsandboxed — decomposing it into per-phase Temporal
activities is ruled out by the parent epic because several quality gates call `datetime.now()`
and read `os.environ`, both illegal inside the temporalio workflow sandbox that per-phase
workflow-orchestrated activities would require.

The activity's own Temporal-level retry budget is intentionally small
(`_ACTIVITY_RETRY.maximum_attempts=2`, `workflows.py:83-88`) — "re-running a whole design attempt
is expensive," per that policy's own comment. A worker crash partway through an attempt therefore
discards everything computed so far and, on the one retry Temporal grants, starts the whole
attempt — design phase included — completely over, re-burning every LLM call already paid for.

This ADR designs a checkpoint at the one boundary inside `_run_design_attempt` most worth saving:
the point where Phase 1 (`_orchestrate_design_and_review`, up to `design_review_rounds` review
round-trips) hands off `spec`/`rationale`/`design_context` to Phase 1b
(`_synthesize_initial_code`) (`orchestrator_design.py:1248-1279`). The design phase is the single
largest, most re-entry-prone, most expensive-to-redo piece of one attempt; checkpointing exactly
there bounds a crash's blast radius to "at most everything from code synthesis onward," without
requiring the ruled-out per-phase decomposition.

Two existing invariants constrain the design:

1. **Fencing** (`activities.py`'s module docstring): only `persist_run_state_activity` and
   `finalize_cycle_record_activity` currently perform fencing-checked durable writes tied to a
   run. Both do so via a **check-then-write**, explicitly documented as *not* atomic — the
   fencing read and the eventual write are two separate job-service calls, leaving a narrow race
   window a restart can (rarely) land in.
2. **Retry state isolation** (`RETRY_STATE_ISOLATION.md`): every retry boundary in Strategy Lab
   follows **copy-on-entry, commit-on-completion** — each attempt works on an isolated copy of
   mutable state, and the parent's authoritative state updates only once the attempt's fate is
   known. A failed attempt's state must never leak into the next attempt.

Any new checkpoint mechanism must fit inside both without weakening either.

## Decision

### Checkpoint identity and scope

A checkpoint is identified by at least `(run_id, design_attempt index)`, and is valid **only**
under the fencing generation it was minted under (`run_state.get_run_generation_strict`'s value
at write time). Two hard scoping rules follow directly from the retry-state-isolation contract
above and must hold regardless of how the implementation sub-issue shapes the storage key:

- **Never cross-attempt.** A checkpoint written while running `design_attempt=N` is never read
  or considered while running `design_attempt=N+1`. A design re-entry (`SpecImplementabilityError`
  → `{"kind": "reentry", ...}`) is a *new* attempt with its own fresh copy-on-entry state
  (fresh `_DriftCollector.snapshot()`, fresh `_backtest_cache`/`_benchmark_bars_cache`/
  `_last_anomaly_check` reset at the top of `_run_design_attempt`,
  `orchestrator_design.py:1222-1238`) — reusing a prior attempt's checkpointed (and, by
  definition, since it triggered re-entry, ultimately-rejected) spec would silently reintroduce
  exactly the cross-attempt leakage `RETRY_STATE_ISOLATION.md` rules out.
- **Never survive a generation bump.** `restart_strategy_lab_run` mints a new generation and
  performs a **full reset** of run-level progress (`contiguous_cycles=0`, counters reset). A
  checkpoint minted under an older generation is stale the instant a restart happens, by the same
  logic `persist_run_state_activity`/`finalize_cycle_record_activity` already apply to their own
  writes: honoring it would let a superseded incarnation's progress leak into a fresh one.

The exact storage key shape (e.g. whether it also folds in a cycle-scoped correlation id) is left
to the implementation sub-issue; it must meet this granularity floor.

### What's persisted at the boundary

Everything `_run_design_attempt`'s Phase 1 produces plus the state needed to resume Phase 1b
onward without redoing or double-charging Phase 1's work:

| Field | Source | Why it's needed on resume |
|---|---|---|
| `spec`, `rationale`, `design_context` | `design_phase` result (`orchestrator_design.py:1264-1266`) | The Phase 1b input Phase 1 exists to produce. |
| Attempt-local drift as of the boundary | `drift_collector`'s state after Phase 1 (design-phase `SpecRevision`/`GateEvent` entries only) | Seeds the *same* attempt-local collector Phase 1b onward keeps mutating — see Reconciliation below. |
| The design-phase slice of `cumulative_gate_results` | `all_gate_results` as mutated by Phase 1 | Preserves the intentionally-cumulative gate-result invariant (see Reconciliation) without recomputing design-phase gates. |
| `budget.calls_made` as of the boundary | `LLMCallBudget` | Resuming must seed the budget from *this* count, not from `params["budget_calls"]` (the pre-attempt count) — see Correctness requirement below. |

`config` is not re-checkpointed separately: Phase 1 does not mutate it (only Phase 1b/synthesis
can, per `code_synthesis.config` at `orchestrator_design.py:1285`), so the original `params`
input remains valid on resume.

### Where the write happens

The checkpoint write is **not** a second Temporal activity dispatched via
`workflow.execute_activity`. The parent epic explicitly rules out decomposing
`run_design_attempt_activity` into multiple activities (the sandbox restriction on
`datetime.now()`/`os.environ` reads inside quality gates, per `workflows.py`'s module docstring,
lines 12-23); a checkpoint-write activity invoked mid-attempt from *inside* another activity's
body would not even be a valid Temporal call shape (activities do not invoke other activities).

Instead, the write is a **plain synchronous call to the durable store**, made inline inside
`run_design_attempt_activity`'s own execution — the same execution context that already performs
cooperative-cancellation heartbeating via a background thread. Because `_run_design_attempt`
itself lives in `orchestrator_design.py` and is reused verbatim by both thread mode and Temporal
mode (per `activities.py`'s module docstring: "each [activity] reconstructs the relevant Pydantic
model(s) ... and calls the existing method verbatim"), the write must be threaded in as an
**optional callback parameter** at the Phase 1 / Phase 1b boundary — the same shape as the
existing `emit: PhaseCallback` parameter already threaded through every phase for cancellation
checkpointing. Thread mode passes a no-op (or omits the callback, defaulting to no-op); Temporal
mode's `run_design_attempt_activity` passes the real durable-write closure, exactly as it already
builds `_beat`/`_design_attempt_cancellation_checkpoint` closures today. This keeps
`orchestrator_design.py` free of any hard dependency on job-service or Temporal internals, and
keeps the write inside the activity executor (never inside the workflow sandbox), matching the
sandbox-safety posture the rest of `activities.py` already relies on.

### Fencing

The checkpoint write and read are fencing-checked exactly like `persist_run_state_activity`:
reuse `shared.fencing.check_fencing_token` for the comparison and
`run_state.get_run_generation_strict` for the fail-closed current-generation read (raises rather
than defaulting to the most permissive generation on a transient durable-read failure). This
contract inherits the same documented non-atomicity as the rest of the fencing surface: the
generation check and the checkpoint write are two separate calls, so a restart racing exactly
between them is (rarely) still possible. This ADR does not attempt to close that window — doing
so would require the shared record-persistence layer to become generation-aware in a genuinely
atomic conditional write, which is out of scope here exactly as it already is for the two
existing fencing-checked activities.

### Resumability semantics

"Resume" means precisely this:

1. **When it's checked.** `run_design_attempt_activity` looks up a checkpoint for its own
   `(run_id, design_attempt)` key at the start of its execution — whether that execution is the
   first Temporal-level attempt or `_ACTIVITY_RETRY`'s one allowed retry (the retry is, from
   Temporal's perspective, a fresh invocation of the activity function; nothing about the crashed
   invocation's in-memory state survives it — the checkpoint is what bridges the gap).
2. **What a valid checkpoint changes.** Phase 1 (`_orchestrate_design_and_review`) is skipped
   entirely — `spec`/`rationale`/`design_context` and the boundary-time attempt-local drift/
   gate-results slice/budget count are seeded from the checkpoint instead of computed. Execution
   resumes at Phase 1b and runs every phase after the boundary **in full**:
   synthesis → refinement/alignment → verification/analysis → record assembly. This is a single
   boundary, not phase-granular checkpointing — a crash during any post-boundary phase still
   discards that portion of the (resumed) attempt and, on the next retry, resumes again from the
   same design/synthesis checkpoint, not from wherever the post-boundary crash occurred.
3. **What it does not change.** `_ACTIVITY_RETRY.maximum_attempts=2` is unchanged — a checkpoint
   makes the one retry Temporal already grants cheaper to use, it does not grant more retries.
   Heartbeat/cancellation machinery (`BackgroundHeartbeat`,
   `_design_attempt_cancellation_checkpoint`) is unaffected — it wraps the same call, checkpoint
   or not. `finalize_cycle_record_activity`'s separate, wider non-atomicity window is untouched,
   matching the parent epic's own out-of-scope boundary.
4. **Cleanup.** The checkpoint for a `(run_id, design_attempt)` key is deleted on **any** terminal
   outcome of that attempt — `"record"`, `"reentry"`, `"skipped"`, or a non-retryable mapped
   error — not only on success. Design re-entry is the case that makes this matter: without
   deleting on `"reentry"` too, a cycle with `MAX_DESIGN_REENTRIES=2` re-entries would accumulate
   one stale checkpoint per prior attempt, none of which will ever legitimately be read again
   (rule one, above), but which would sit as durable-store cruft indefinitely. Cleanup is
   best-effort: a crash between assembling the terminal outcome and issuing the delete leaves one
   orphaned checkpoint keyed to a `design_attempt` index the workflow will never revisit (the
   outer loop only moves forward), so it is inert clutter, not a correctness hazard. No
   reaper/TTL is proposed here; if orphaned-checkpoint accumulation proves operationally
   relevant, that is a separate follow-up.

### Correctness requirement: no double-charged LLM budget on resume

This is the concrete mechanism behind the parent epic's "assert no double-charged LLM budget on
resume" acceptance criterion. Two things combine to make this hold structurally, not just by
convention:

- Skipping Phase 1 entirely on a valid-checkpoint resume means the design phase's LLM calls are
  never re-issued — there is nothing to double-charge, because the calls simply don't happen a
  second time.
- The resumed `LLMCallBudget` is seeded from the checkpoint's `budget.calls_made` value (the count
  *as of the boundary*, including every design-phase call), not from `params["budget_calls"]`
  (the count as of the *start* of this attempt, before any of its own design-phase calls). Seeding
  from the pre-attempt count on a resume would silently reopen budget headroom for calls that
  already happened and won't happen again — not a double charge, but a budget-ceiling
  under-count with the same practical failure mode (the cycle's true LLM spend exceeding
  `STRATEGY_LAB_DESIGN_MAX_LLM_CALLS` without the check catching it). Seeding from the
  checkpoint's boundary-time count keeps the ceiling accurate regardless of whether this
  particular attempt execution is a first pass or a checkpoint resume.

## Rejected alternatives

- **Phase-granular checkpointing at every existing `emit` boundary** (design, synthesis,
  refinement, alignment, verification, analysis — the same six checkpoints
  `_design_attempt_cancellation_checkpoint` already visits for cancellation). Rejected: the parent
  epic scopes this work to the design/synthesis boundary specifically, and phase-granular
  checkpointing multiplies the durable-write surface, the fencing-check surface, and the
  resume-reconstruction logic six-fold for phases whose LLM cost is individually much smaller than
  the design phase's up-to-540-call worst case. A single boundary captures most of the available
  savings for a small fraction of the surface area; extending to other boundaries is a natural,
  separate future increment if warranted.
- **A dedicated Temporal activity for the checkpoint write**, invoked from the workflow between
  Phase 1 and Phase 1b. Rejected outright: this is exactly the per-phase decomposition the parent
  epic's Out of Scope section rules out, and for the same reason — it would require re-expressing
  the quality gates' `datetime.now()`/`os.environ` usage as sandboxed workflow-safe code, or
  moving the workflow/activity boundary in a way that reintroduces the determinism hazards
  `workflows.py`'s module docstring documents as the reason the whole attempt runs as one
  activity today.
- **Letting a checkpoint remain valid across a generation bump**, relying only on
  `(run_id, design_attempt)` without the generation check. Rejected: this would be strictly weaker
  than the fencing guarantee every other durable, run-tied write in this system already provides,
  and would contradict `restart_strategy_lab_run`'s existing "full reset" semantics — a restart
  resets `contiguous_cycles` and other run-level progress specifically so a fresh incarnation
  starts clean; silently resuming a design attempt from a pre-restart checkpoint would undermine
  that guarantee for exactly the state this ADR adds.
- **Seeding the resumed budget from `params["budget_calls"]`** (the simpler, already-available
  pre-attempt count) rather than a checkpoint-carried boundary-time count. Rejected per the
  Correctness requirement above — it would silently under-count the true per-cycle LLM spend on
  every checkpoint resume.

## Risks and tradeoffs

- **Bounded savings.** Only the design phase's cost is ever recovered by this checkpoint. A crash
  during synthesis, refinement, alignment, verification, or analysis still discards that entire
  portion of the (possibly already-resumed) attempt, exactly as today. This is a deliberate,
  epic-scoped tradeoff, not an oversight — see Rejected alternatives above.
- **Inherited non-atomicity.** The checkpoint write is a check-then-write against the fencing
  generation, with the same narrow race window every other fencing-checked write in this system
  already has and already accepts. This ADR does not raise or lower that bar.
- **Best-effort cleanup.** Orphaned checkpoints from a crash between terminal-outcome assembly and
  delete are possible, unbounded by any TTL, and rely entirely on being provably inert (never
  re-read, because rule one above forbids reading a prior attempt's checkpoint) rather than being
  actively reclaimed.
- **New attempt-local state to reconstruct correctly.** Resume must rebuild the attempt-local
  `_DriftCollector` and `cumulative_gate_results` slice from checkpointed data in a way that is
  bit-for-bit equivalent (from the rest of the pipeline's point of view) to what a non-resumed
  attempt would have built in memory at the same point — any divergence risks reintroducing the
  cross-attempt leakage this contract is designed to avoid. The implementation sub-issue's test
  coverage is the enforcement point for this; this ADR specifies the shape but does not itself
  prove the reconstruction correct.

## Reconciliation with `RETRY_STATE_ISOLATION.md`

`RETRY_STATE_ISOLATION.md` documents copy-on-entry/commit-on-completion for exactly one kind of
boundary: the boundary *between* attempts (design re-entry, alignment rounds, zero-trade-repair
proposals). This checkpoint is a different kind of boundary — *within* one attempt, purely to
survive a worker crash — and is designed to compose with, not replace or weaken, that existing
contract:

- **The checkpoint is attempt-local state**, exactly like the `attempt_drift` a re-entry loop
  already snapshots via `drift_collector.snapshot()`. Checkpointing it mid-attempt does not
  change *when* it becomes visible to the parent: the parent workflow's drift collector is still
  only ever updated via the existing boundary `merge()` call, on this attempt's eventual success
  or failure — never by the checkpoint write itself. A checkpoint resume seeds the *same*
  attempt-local collector that a non-resumed execution would have built from scratch; it does not
  create a new merge point.
- **Rule one above (never cross-attempt) is the same guarantee `RETRY_STATE_ISOLATION.md`
  already states**, applied to a new mechanism: "a failed attempt's ... mutations [never leak]
  into the next attempt's reasoning." A checkpoint from a design attempt that is later abandoned
  (via re-entry) is deleted on that attempt's terminal outcome and never consulted by the
  attempt that replaces it — so the checkpoint mechanism cannot become a second, undocumented
  channel for exactly the leakage `RETRY_STATE_ISOLATION.md` was written to rule out.
- **The checkpointed `cumulative_gate_results` slice is consistent with the existing
  "intentionally cumulative" callout** in `RETRY_STATE_ISOLATION.md`: that document already notes
  `cumulative_gate_results` spans all attempts on purpose, feeding the deflated-Sharpe
  multiple-testing burden. This checkpoint does not change that accounting — it only changes
  *when* the design phase's contribution to that already-cumulative list becomes durable, so a
  resumed attempt doesn't have to redo gate evaluation to reproduce entries it already recorded
  once.
- **No change to the "Locked in by" table** in `RETRY_STATE_ISOLATION.md` is made by this ADR —
  that table tracks tests for landed behavior, and this issue ships no code. The implementation
  sub-issue should add both the new prose section (added alongside this ADR) and a
  corresponding "Locked in by" row once its tests land.

## Contract boundary

A future implementation must satisfy exactly this surface:

- A checkpoint keyed at least by `(run_id, design_attempt)`, valid only under the fencing
  generation active at write time, read and written via a plain synchronous durable-store call
  inline inside `run_design_attempt_activity` — never a second Temporal activity.
- The write and read both go through `shared.fencing.check_fencing_token` /
  `run_state.get_run_generation_strict`, inheriting that surface's existing check-then-write
  non-atomicity rather than inventing a new consistency model.
- Persisted fields: `spec`, `rationale`, `design_context`, the attempt-local drift state as of the
  boundary, the design-phase slice of `cumulative_gate_results`, and `budget.calls_made` as of the
  boundary.
- `_run_design_attempt` (or a thin wrapper around it) gains a way to skip Phase 1 and start at
  Phase 1b when seeded with checkpointed state — most naturally an optional parameter alongside
  the existing `emit`, `drift_collector`, `cumulative_gate_results` parameters it already accepts,
  keeping the "verbatim, mode-agnostic" property `activities.py`'s module docstring calls out for
  thread mode.
- The checkpoint-write hook is threaded into `orchestrator_design.py` as an optional callback,
  defaulting to a no-op, so thread mode's call path is unaffected by its existence.
- Checkpoint deletion fires on every terminal outcome of the attempt it belongs to
  (`"record"`, `"reentry"`, `"skipped"`, non-retryable error), not only on success.
- `RETRY_STATE_ISOLATION.md` gains a "Locked in by" row once tests land; the module docstring in
  `activities.py` and `strategy_lab/README.md` gain the behavior/guarantee documentation the
  parent epic's own acceptance criteria call for — both are the implementation sub-issue's
  responsibility, not this ADR's.

## Consequences

- **The design question is closed, not deferred.** The checkpoint's location (design/synthesis
  boundary), identity/scoping rules, persisted-field set, write mechanism (inline synchronous
  call via an optional callback, not a second activity), fencing treatment, and resume/cleanup
  semantics are all specified here, reconciled explicitly against `RETRY_STATE_ISOLATION.md`'s
  existing invariants.
- **No behavior changes as a result of this ADR.** No code, tests, or durable schema changes ship
  in this issue.
- **The implementation sub-issue's job is narrowed to plumbing.** It builds the checkpoint-write/
  read helper, threads the new optional callback through `_run_design_attempt`, wires
  `run_design_attempt_activity` to check for and resume from a checkpoint, adds the crash-and-
  resume + no-double-charge test coverage the parent epic's acceptance criteria require, and adds
  the deferred documentation (README, module docstring, `RETRY_STATE_ISOLATION.md`'s "Locked in
  by" row) — all against the contract fixed here, not a contract it still needs to design.
