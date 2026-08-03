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

## Locked in by

| Contract | Tests |
| --- | --- |
| `_DriftCollector.snapshot()` / `merge()` semantics | `tests/test_strategy_lab_drift_observability.py` |
| Failed design attempt does not poison the next; short-circuit record preserves failed-attempt drift | `tests/test_strategy_lab_phase_transitions.py` |
| `ZeroTradeRepairer` purity w.r.t. input code/spec | `tests/test_strategy_lab_zero_trade_repair.py` |
| Rejected alignment proposal preserves known-good state | `tests/test_strategy_lab_alignment.py` |
