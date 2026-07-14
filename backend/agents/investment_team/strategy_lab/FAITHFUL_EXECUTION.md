# Faithful Execution Defences

Strategies are only worth backtesting if the **executed trades implement the authored
specification**. When they don't, the alignment audit stamps the run "The executed trades did
not faithfully implement the specification; interpretation is preliminary" and the cycle is
wasted. This document records the defences that keep generated strategies faithful, and the
follow-up work still open.

## Root cause: two execution paths

A spec runs on one of two paths, chosen by `spec.requires_custom_code`
(`mechanical_repair.py:select_code_path`, `synthesis/compiler.py`):

- **Path A — deterministic compiler (default).** `compile_strategy(spec)` emits a thin
  `on_bar` shim that submits **zero** orders; every entry/exit is decided engine-side by
  `_EngineEntryDispatcher` / `_EngineExitDispatcher` reading the spec through
  `executor/predicate_evaluator.py`. **Faithful by construction.**
- **Path B — LLM-authored custom code (`requires_custom_code=True`).** An LLM writes `on_bar`
  by hand (`agents/code_synthesis.py`). This is where every faithful-execution defect
  originates: reading an indicator on the wrong `source` (`low` vs `close`), excluding the
  current bar, disabling a spec condition with a falsy guard, or crashing on a non-existent
  attribute (`position.quantity`).

The guiding principle: **keep DSL-expressible specs on Path A, and hard-contract Path B code
against the spec when custom code is genuinely required.**

## Implemented defences

### 1. Path-B spec-conformance contract (`quality_gates/code_conformance.py`)

`CodeConformanceGate._check_custom_code_faithfulness` rejects, pre-execution, the three ways
custom `on_bar` code diverges from the spec — turning a wasted "preliminary" backtest into a
synthesis-retry:

- **Indicator source/params divergence** — `_divergent_ctx_indicator_reads` asserts each
  `ctx.indicator('<name>', ...)` read matches an authored `IndicatorRef` on `source` and
  params (not merely that the source is *valid*). Catches `source='low'` against a
  `source='close'` spec. Lenient on dynamic/unpinned fields and on indicators the spec does
  not require.
- **Falsy guard on an indicator value** — `_indicator_falsy_guard_errors` rejects
  `if vol_sma and ...` / `if not vol_sma`, requiring an explicit `is None` check so a
  legitimate `0.0` (a flat-window volume SMA) still gates the order.
- **Non-existent position attribute** — `_invalid_position_attr_errors` rejects reads outside
  `_POSITION_SNAPSHOT_ATTRS` (e.g. `pos.size`), which would `AttributeError` at runtime.

All three fire only on `ctx.`-accessor (custom) code; the compiler emits named calls with
spec-matched sources, so compiled strategies never trip them.

### 2. `quantity` alias on the position snapshot (`trading_service/strategy/contract.py`)

`_PositionSnapshot.quantity` is a read-only alias for `qty`. LLM code routinely reaches for the
natural name `position.quantity`; without the alias that read crashed the whole backtest. The
alias makes the natural name a faithful synonym; the gate above still rejects genuinely-unknown
attributes.

### 3. Self-healing alignment loop (`agents/alignment.py`)

The fix-proposer loop no longer fails closed on a metadata parse error:

- `_format_findings_section` renders `trade_num=N` instead of a copyable `trade #N` token the
  LLM pasted verbatim into the integer `affected_trades` field.
- `_coerce_affected_trades` coerces whatever the LLM echoes (`["trade #1"]`, `"7"`, `1.0`) into
  `List[int]`, so `_coerce_report` never raises on a malformed issue.
- `propose_code_fix` wraps report coercion to **fail open**: a residual error preserves the
  LLM's `proposed_code` patch (`aligned` stays `False`) instead of the orchestrator discarding
  it at the `no_proposed_fix` dead end.

### 4. Demote over-elected custom code (`mechanical_repair.py:demote_code_path`)

The inverse of `select_code_path`: in design pre-flight Stage 2, a spec flagged
`requires_custom_code=True` that **compiles cleanly** is demoted back to Path A with a
`compiler_demote` repair action. A `CompilerError` is the authoritative "the DSL cannot express
this" signal, so genuinely cross-asset / path-dependent specs stay on custom code. Gated by
`STRATEGY_LAB_DEMOTE_COMPILABLE_CUSTOM_CODE` (default on).

## Follow-up work (not yet implemented)

### S4 — Data-driven reachability probe before backtest

Closed-form reachability (`spec_readiness.py:_check_predicate_reachability`) catches only
tautologies (`rsi > 100`, `close < close`) — not **data-dependent** dead code: an `all_of`
whose legs never co-occur, or `sma(5) > sma(200)` that never crosses in the window. The AST
coverage probe reads `spec.strategy_code` and is blind to the compiled path (the shim has no
entry `if`s); `realism/rule_firing.py` self-skips custom code and runs only post-hoc as a
caveat. So a strategy can reach backtest, emit zero trades, and only *then* be flagged.

**Proposed:** a pre-backtest probe that evaluates each entry `PredicateTree` against the actual
historical bar window using the same `executor/predicate_evaluator.py` the engine uses, and
reports per-leg firing counts. Runs on both paths; turns "dead predicate / zero trades" into an
authoring-time redesign signal instead of a wasted cycle.

### S5 — Design-time spec-quality hard gates

- **`max_position_pct`**: the DSL field is bounded `le=100`, so an LLM can author `100`
  (default is `6.0`), which three post-hoc gates then flag and mechanical-repair clamps. Bound
  the field to `le=MAX_POSITION_PCT_CEILING` (25) so the out-of-range value is unconstructable.
- **Hypothesis/rules consistency** (`strategy_validator.py`): currently a post-hoc **warning**
  driven by surface-vocabulary matching (`_CONCEPT_TERMS`) of the hypothesis vs. structured
  `IndicatorRef` names. Promote it to a design-review critique so `DesignAgent.revise` must
  reconcile narrative and DSL before synthesis, and broaden the vocabulary / compare structured
  refs on both sides to cut false orphans.

## Test coverage

- `tests/test_code_conformance_gate.py` — the three faithfulness checks (reject + pass), the
  `quantity` alias, and the allowlist↔model sync.
- `tests/test_indicator_accessor.py` — source-divergence critical; exact-match pass.
- `tests/test_alignment_helpers.py` — `affected_trades` coercion, patch-survives regression,
  `trade_num` render.
- `tests/test_strategy_lab_mechanical_repair.py` — `demote_code_path` unit cases.
- `tests/test_strategy_lab_design_loop.py` — pre-flight demote (on/off) integration.
