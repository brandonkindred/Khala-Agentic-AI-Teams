# Strategy Lab — Retry State Isolation

The Strategy Lab retries work at several boundaries: the design phase re-enters on a
`SpecImplementabilityError`, the synthesis loop refines code over multiple rounds, the trade
alignment loop proposes fixes round over round, and the zero-trade repairer attempts a
targeted patch. Long-lived mutable structures are threaded through these loops to build the
final diagnostic record. Without discipline, state from a **failed** attempt leaks into the
**next** attempt's reasoning — contaminating diagnostics, blocking safe parallelisation, and
making a failed repair's partial mutations visible to the next agent.

The contract is **copy-on-entry, commit-on-completion**: each retry attempt works on an
isolated copy of the mutable state, and the parent's authoritative state is only updated once
the attempt's fate is known.

## Drift collection across design attempts

`_DriftCollector` (`_orchestrator_helpers.py`) accumulates `SpecRevision` / `CodeRevision` /
`GateEvent` records that are drained into `StrategyLabRecord` at record-build time. Its
records are **append-only and immutable**, so isolating an attempt does not require deep-copying
history — it means giving the attempt its own empty collector:

- `snapshot()` returns a **fresh, empty** `_DriftCollector` sharing no list object with its
  parent — the clean working copy for one attempt.
- `merge(child)` folds a child's records into the parent in order, leaving the child intact.

`run_cycle` owns a parent `drift_collector` that serves purely as the **commit log** for the
short-circuit diagnostic record. The design re-entry loop:

1. **Copy-on-entry** — `attempt_drift = drift_collector.snapshot()` before each attempt; the
   attempt records only into `attempt_drift`.
2. On **success**, `_run_design_attempt` builds the record from `attempt_drift`, so a converged
   run's drift reflects only the successful attempt — prior failed attempts never leak in.
3. On **failure** (`SpecImplementabilityError`), **commit-on-completion** —
   `drift_collector.merge(attempt_drift)` folds the failed attempt's drift into the parent so
   the short-circuit record retains it for diagnostics. The *next* attempt's `snapshot()` is
   still a fresh empty child, so the merge never contaminates it.

The parent collector is never appended to from inside an attempt; the only mutation of the
parent is the explicit `merge` at the boundary.

> Intentionally cumulative (not isolated): `cumulative_gate_results` in `run_cycle` spans all
> attempts on purpose — it feeds the deflated-Sharpe multiple-testing burden, where each failed
> attempt's gate work *should* count.

## Zero-trade repair purity

`ZeroTradeRepairer.try_repair()` (`zero_trade_repair.py`) is **pure with respect to the
caller's `code` and `spec`**: it builds a fresh proposed spec/code internally and never mutates
the inputs. It returns a `ZeroTradeRepairOutcome` that either commits (`committed=True`, new
state on the outcome) or rejects (`committed=False`, empty/`None` outcome fields). On rejection
the caller falls through to the generic `RefinementAgent` against the **original** code blob,
not a half-repaired one. The `zero_trade_attempts` list is an audit log appended on both
commit and rejection — analogous to the drift commit log, not part of the working code state.

## Alignment round state

`_AlignmentRoundOutcome` (`orchestrator_alignment.py`) carries the spec/code/trades/metrics for
one alignment round. On a rejected proposal (`terminate=True`) the outcome carries the **same
pre-iteration spec/code objects** — the known-good state survives a failed attempt untouched;
a committed proposal (`terminate=False`) carries the new known-good state. Issue lists are
re-derived deterministically per round from the structured findings
(`findings_to_issues(check_result.findings)`), never accumulated across rounds.

## Intra-attempt checkpointing (design/synthesis boundary)

`ADR-012` (`system_design/adr/ADR-012-strategy-lab-design-attempt-checkpoint-contract.md`) adds a
second, different kind of boundary on top of the attempt-to-attempt contract above: a durable
checkpoint *within* one design attempt, taken where Phase 1 (design + review) hands off to
Phase 1b (code synthesis), so a worker crash mid-attempt can resume past the design phase instead
of discarding the whole attempt. It is a checkpoint of attempt-local state, not a new kind of
cross-attempt state — and is designed to compose with, not weaken, the copy-on-entry/
commit-on-completion contract this document describes:

- **It never crosses the attempt-to-attempt boundary this document governs.** A checkpoint is
  scoped to exactly one `(run_id, design_attempt)` pair and is never read by a different
  `design_attempt` index. A design re-entry after `SpecImplementabilityError` still starts from
  a fresh `snapshot()`-ed drift collector and fresh attempt-scoped caches, exactly as described
  above — a checkpointed (and, by definition, since it triggered re-entry, ultimately-rejected)
  spec from the failed attempt is never consulted by the attempt that replaces it. This is the
  same "a failed attempt's state never leaks into the next attempt's reasoning" guarantee this
  document states, applied to the new mechanism.
- **It does not add a new merge point into the parent drift collector.** The checkpointed drift
  is the same attempt-local `attempt_drift` this document already describes as copy-on-entry;
  checkpointing it mid-attempt (to survive a crash) does not change when it becomes visible to
  the parent — that still happens only via the existing boundary `merge()` call, on this
  attempt's eventual success or failure, never from the checkpoint write itself.
- **The checkpointed gate-results slice is consistent with the "intentionally cumulative"
  callout above.** `cumulative_gate_results` already spans all attempts on purpose, feeding the
  deflated-Sharpe multiple-testing burden. Checkpointing the design phase's contribution to that
  list changes only when it becomes durable, not the accounting itself — a resumed attempt
  reuses its own already-recorded gate results rather than re-evaluating them.
- **Cleanup mirrors the same discipline**: a checkpoint is deleted on any terminal outcome of the
  attempt it belongs to (record, reentry, or skip) — not just success — so a design-re-entry
  loop never accumulates checkpoints from attempts that will never be revisited.

See `ADR-012` for the full contract: checkpoint identity/scoping, the persisted-field set,
fencing treatment, and resumability semantics. The checkpoint-persistence write path and the
read/resume-on-crash wiring into `run_design_attempt_activity` have now landed (see the
`DesignAttemptCheckpoint`/`persist_design_attempt_checkpoint`/`load_design_attempt_checkpoint`
row below); checkpoint cleanup on terminal outcome is tracked as a separate, still-open
follow-up.

## Locked in by

| Contract | Tests |
| --- | --- |
| `_DriftCollector.snapshot()` / `merge()` semantics | `tests/test_strategy_lab_drift_observability.py` |
| Failed design attempt does not poison the next; short-circuit record preserves failed-attempt drift | `tests/test_strategy_lab_phase_transitions.py` |
| `ZeroTradeRepairer` purity w.r.t. input code/spec | `tests/test_strategy_lab_zero_trade_repair.py` |
| Rejected alignment proposal preserves known-good state | `tests/test_strategy_lab_alignment.py` |
| Intra-attempt checkpoint write/read scoping (`cycle_scope`-disambiguated, generation-fenced), checkpoint-resume skips Phase 1 and never double-charges the LLM budget | `tests/test_strategy_lab_temporal_activities.py`, `tests/test_strategy_lab_phase_transitions.py` |
