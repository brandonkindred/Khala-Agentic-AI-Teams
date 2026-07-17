# Strategy Lab: Gate Paper-Trading on `is_publishable`

**Status:** Approved 2026-07-17  
**Date:** 2026-07-17  
**Type:** Contract / decision-gate fix for Strategy Lab publication  
**Issue:** GitHub #1564 (PR body only; do not cite in code)

## Problem

Today the Strategy Lab "winner" verdict is purely a return threshold:

```python
is_winning = execution_succeeded and trades and result.annualized_return_pct >= 8.0
```

`_apply_publication_vetoes` runs realism, cost-stress, trade-clustering, alignment, and exit-rule-conformance critical checks — but each veto only appends to `metrics.acceptance_reason` and **never** flips `is_winning`. Per `system_design/strategy_lab_pipeline.md`, paper-trading gates on `is_winning`.

**Consequence:** a strategy whose edge collapses at 2× cost stress, that concentrates trades in one quarter, or whose ledger does not implement the spec, can still be labeled winning **and paper-traded**.

A secondary doc-drift bug: `quality_gates/cost_stress_realism.py` (and `realism/__init__.py`) claim criticals "veto `is_winning`", which is false.

## Goals

1. Paper-trading only runs on strategies that clear the return threshold **and** the existing robustness/realism/alignment/conformance/lookahead gates.
2. Keep `is_winning` as a pure return-threshold reporting label.
3. Introduce `is_publishable` as the decision that gates execution.
4. Persist non-publishable-but-winning records with an explicit skip reason naming the failing gate(s).
5. Correct the false "vetoes `is_winning`" docstrings.

## Non-goals

- Changing the 8% return threshold or winner-gate math.
- Adding new robustness/realism gates.
- Any change to the paper-trading engine internals.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Approach | Compute in verification, persist on `StrategyLabRecord` | Single source of truth; matches AC; works with Temporal finalize |
| Skip reason format | Gate-specific codes, joined when multiple fail | Mirrors `acceptance_reason` veto suffixes; audit-friendly |
| Multi-fail ordering | Veto order: conformance → realism → alignment → lookahead | Same fixed order as `_apply_publication_vetoes` |
| Legacy rows | Missing `is_publishable` → `False` | Safer; old winners must re-run a cycle to get a publishable verdict |
| Do not flip `is_winning` | Vetoes remain caveats on `acceptance_reason` only | Preserves "winning but low quality" reporting |

Rejected alternatives:

- **Re-derive at paper-trade time** from gate results / `acceptance_reason` — fragile parsing; Temporal/API drift; fights "record persists with `is_publishable=False`".
- **Flip `is_winning` on veto** — out of scope; collapses reporting and publication into one bit.

## Architecture

```
_run_verification_phase
  ├─ resolve is_winning          # return threshold only (unchanged)
  ├─ _apply_publication_vetoes   # stamps acceptance_reason only (unchanged)
  └─ resolve is_publishable      # NEW: and of gate booleans
       └─ persist on StrategyLabRecord

_finalize_strategy_lab_record / POST /strategy-lab/paper-trade
  └─ gate on is_publishable (not is_winning alone)
```

### Formula

```text
is_publishable = (
  is_winning
  and realism_passed
  and trades_aligned
  and exit_rule_conformance_passed
  and not runtime_lookahead_violation
)
```

Computed in the orchestrator after existing veto bookkeeping, from the same gate results already available in `_run_verification_phase`.

### Data model

Add to `StrategyLabRecord`:

- `is_publishable: bool = False` — default `False` so missing/legacy rows deserialize as not publishable.

`_VerificationOutcome` gains `is_publishable` plus the four gate booleans needed to rebuild the skip reason (or a single precomputed `publishability_skip_reason: Optional[str]`). `run_cycle` / Temporal finalize persist `is_publishable` the same way as `is_winning`.

**Chosen wiring:** one shared helper (e.g. `publishability_skip_reason(...)`) used by:

1. Integrated finalize — when skipping for non-publishable, set `paper_trading_skipped_reason` from the helper.
2. Standalone `POST /paper-trade` — if `not is_publishable`, 400 with detail from `record.paper_trading_skipped_reason` when already set by a prior cycle; otherwise a generic "not publishable" message (covers legacy rows that never recorded gate codes).

### Skip reasons

When `is_winning` is False → existing `not_winning` (unchanged).

When `is_winning` is True and `is_publishable` is False → `paper_trading_skipped_reason` is the comma-joined list of failing codes in veto order:

| Code | When included |
|---|---|
| `exit_rule_conformance_failed` | `exit_rule_conformance_passed` is False |
| `realism_failed` | `realism_passed` is False |
| `alignment_unresolved` | `trades_aligned` is False |
| `lookahead_violation` | `runtime_lookahead_violation` is True |

Example: `exit_rule_conformance_failed,realism_failed`.

Existing reasons unchanged: `disabled`, `no_strategy_code`, `no_market_data`.

### Paper-trading entry points

**Integrated cycle** (`api/main.py` finalize path):

1. `not is_winning` → skip `not_winning`
2. `is_winning and not is_publishable` → skip with joined gate codes; never enter paper-trade
3. Then existing: `disabled` → `no_strategy_code` → run / `no_market_data` / `failed`

**Standalone** `POST /strategy-lab/paper-trade`:

- Keep HTTP 400 for non-winning.
- Add HTTP 400 for winning-but-not-publishable (including legacy default `False`), with detail naming the joined reason when available.
- No changes inside the paper-trading engine.

## Documentation

- Update `system_design/strategy_lab_pipeline.md`: split winner label vs publishable decision; update mermaid + skip-reason table; remove "is_winning is the single source of truth" for paper-trading.
- Update `system_design/paper_trading_integration.md` in lockstep.
- Fix `cost_stress_realism.py` module docstring (and `realism/__init__.py` if needed): criticals contribute to `is_publishable` / `acceptance_reason`, they do **not** flip `is_winning`.

## Testing

1. Negative Sharpe at 2× cost → `is_winning=True`, `is_publishable=False`, paper-trade skipped with `realism_failed` in the reason.
2. Misaligned trades → same pattern with `alignment_unresolved`.
3. Fully clean winner → `is_publishable=True` and paper-trade still runs.
4. Regression: realism critical keeps `is_winning=True`; extend existing caveat tests for `is_publishable=False`.
5. Legacy / default: missing `is_publishable` deserializes to `False`; standalone endpoint rejects it.

Backend coverage stays ≥90% on touched code.

## Implementation sketch (for the plan)

1. Model + `_VerificationOutcome` fields.
2. Orchestrator: compute `is_publishable` + shared skip-reason helper; thread through `run_cycle`.
3. Wire both paper-trade gates in `api/main.py` (and Temporal finalize if it shares that path).
4. Docs + docstring fixes.
5. Tests above.
