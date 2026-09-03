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

### 5. Data-driven reachability probe before backtest (`quality_gates/predicate_reachability.py`)

Closed-form reachability (`spec_readiness.py:_check_predicate_reachability`) catches only
tautologies (`rsi > 100`, `close < close`) — not **data-dependent** dead code: an `all_of`
whose legs never co-occur, or `sma(5) > sma(200)` that never crosses in the window. The AST
coverage probe reads `spec.strategy_code` and is blind to the compiled path (the shim has no
entry `if`s); `realism/rule_firing.py` self-skips custom code and runs only post-hoc as a
caveat. So a strategy could reach backtest, emit zero trades, and only *then* be flagged.

`PredicateReachabilityProbe` runs in the synthesis loop **before** the backtest and evaluates
each entry rule's authored `PredicateTree` against the real fetched bars using the same
`evaluate_tree` the compiled engine uses. It reports per-rule and per-leg firing counts,
excluding warm-up bars and abstaining when there are too few post-warmup bars to judge. Because
it uses the engine's own evaluator, on the compiled path "zero fires" provably means "zero entry
orders" → the finding is **critical**; on the custom path (executed code may differ from the
spec) it is a **warning**. `all_entries_dead()` distinguishes "the strategy can generate no
entry at all" from "one rule is dead but others fire", and the per-leg diagnostic names the
bottleneck (a leg that never holds, or legs that never co-occur). Findings are recorded on the
gate timeline; routing stays with the existing post-backtest zero-trade path.

This probe currently only catches a rule that never fires at all ("dead"). It does not yet catch
a rule that fires plenty on its own but is always shadowed by an earlier, higher-priority rule in
`evaluate_entry_rules`'s first-match-wins scan ("structurally starved") — that extension is
tracked separately. The authoritative priority decision and the formal "structurally starved"
definition it must detect are documented on `evaluate_entry_rules` in
`executor/predicate_evaluator.py`.

### 6. Design-time spec-quality hardening (`design_system.md`, `strategy_validator.py`, `orchestrator.py`)

- **`max_position_pct` ceiling.** The shared `RiskLimits.max_position_pct` field is intentionally
  bounded `le=100` because the **trading engine** uses full-deployment (100%) values in its own
  tests, so it cannot be tightened to 25 without breaking the engine. Instead the 25% ceiling is
  enforced where it belongs — for *specs*: the design prompt now states the ceiling explicitly so
  the LLM never authors `50`/`100`, backed by the existing `SpecReadinessGate` critical and the
  deterministic `mechanical_repair` clamp.
- **Hypothesis/rules consistency.** The check is now **accurate** — the rules side credits
  concepts a rule reads via a bar-field (`bar.volume`) or an indicator `source` (`source='volume'`),
  so a volume filter no longer false-orphans a "volume" hypothesis (`_rule_derived_concepts`). The
  corrected finding is **surfaced to the design reviewer** each round (`check_hypothesis_rules`
  merged into the reviewer's deterministic findings in `_review_and_handle_critique`), so a
  genuine narrative/DSL mismatch is reconciled during the design loop rather than only recorded as
  a pre-synthesis warning. The LLM reviewer adjudicates, so there is no hard-block churn.

## Test coverage

- `tests/test_code_conformance_gate.py` — the three faithfulness checks (reject + pass), the
  `quantity` alias, and the allowlist↔model sync.
- `tests/test_indicator_accessor.py` — source-divergence critical; exact-match pass.
- `tests/test_alignment_helpers.py` — `affected_trades` coercion, patch-survives regression,
  `trade_num` render.
- `tests/test_strategy_lab_mechanical_repair.py` — `demote_code_path` unit cases.
- `tests/test_strategy_lab_design_loop.py` — pre-flight demote (on/off) integration; the reviewer
  receives the hypothesis/rules finding.
- `tests/test_predicate_reachability.py` — reachable/dead/insufficient verdicts, compiled-critical
  vs custom-warning, per-leg diagnostics, `all_entries_dead`.
- `tests/test_strategy_validator.py` — volume-rule false-orphan regression, `_rule_derived_concepts`,
  `check_hypothesis_rules` flag/empty.
