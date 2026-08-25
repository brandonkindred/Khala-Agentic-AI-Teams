# Strategy Generation Pipeline — Inner Mechanics

This is the "how it all works together" deep dive: what actually happens
**inside** a single Strategy Lab design attempt, from a bare cycle request to
an assembled `StrategyLabRecord`. It complements, rather than duplicates,
[`strategy_lab_pipeline.md`](./strategy_lab_pipeline.md), which documents the
*outer* cycle phases and their SSE events. Note the two vocabularies: the
orchestrator emits internal phase names (`designing`, `design_review`,
`design_repair`, `coding`, `backtesting`, `aligning`, `analyzing`,
`complete`, plus the deliberately-unmapped `telemetry` and
`phase_transition`), and `_PROGRESS_PHASE_MAP` collapses the first eight onto
the four-entry stepper a UI client actually receives — `ideating → coding → backtesting →
analyzing` (an unmapped phase is not published at all). `fetching_data` is a
`sub_phase` of `backtesting`, not a phase of its own. This document opens up
what happens behind those — in particular everything inside "ideating" (the
design ↔ review loop) and "backtesting" (synthesis, refinement, verification)
— and is the companion to [`architecture.md`](./architecture.md)'s container
view and §11 (orchestrator composition).

Everything below through "Record assembly" happens inside **one Temporal
activity**, `run_design_attempt_activity`
(`run_design_attempt_activity` in [`../strategy_lab/temporal/activities.py`](../strategy_lab/temporal/activities.py)),
a thin wrapper around
`StrategyLabOrchestrator._run_design_attempt`
(`_run_design_attempt` in [`../strategy_lab/orchestrator_design.py`](../strategy_lab/orchestrator_design.py)).
That activity's own scope ends at record assembly: it returns the assembled
`StrategyLabRecord` (JSON-dumped) to the calling `StrategyLabCycleWorkflow`
as `{"kind": "record", "record": ...}` — it does **not** persist that record
or run paper trading itself. Both of those happen one step later, in
`finalize_cycle_record_activity` (see "Batch / Temporal activity mapping"
below), which is the actual durable-write and paper-trading boundary.
Temporal's durability boundary for everything *inside* this document is the
*design attempt* itself, not any phase inside it — a worker crash mid-attempt
discards everything after the design/synthesis boundary. On the retry Temporal
grants, a valid design-phase checkpoint makes the attempt **skip** the design
phase entirely rather than re-run it (its LLM calls must never be re-issued);
without one, the attempt restarts from the top. See "Design-attempt
checkpointing" in [`../strategy_lab/README.md`](../strategy_lab/README.md).

A file-reference note before diving in: `../strategy_lab/orchestrator.py`
defines the combined `StrategyLabOrchestrator` class itself — composed from
five mixins (see "Orchestrator composition" below) that each own one slice
of the pipeline. Most phase-specific logic below is cited to its owning
mixin file, but several top-level orchestration methods that don't belong to
any single phase — `_synthesize_initial_code` (the compiled-DSL-vs-custom-code
fork), `_run_realism_gates`, `_refine`/`_refine_or_exhaust`, `_run_alignment_audit`,
`_apply_updates` — live directly on `../strategy_lab/orchestrator.py` itself,
not on a mixin. Bare `orchestrator.py` citations below always mean that file,
never the team-level `investment_team/orchestrator.py` (a different, much
smaller module that owns the advisor-track queues).

## The 4-phase contract

[`../strategy_lab/phases.py`](../strategy_lab/phases.py) defines the phase
enum every attempt moves through, in order:

```
DESIGN → DESIGN_REVIEW → CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION
```

All four fire on a fresh attempt. A **checkpoint-resumed** attempt is the
exception: `_run_design_attempt` skips `_orchestrate_design_and_review`
entirely when `resume_spec` is set. Both design-phase emits are reached only
through that call — `DESIGN_REVIEW → CODE_SYNTHESIS` directly inside it, and
`DESIGN → DESIGN_REVIEW` transitively via `_run_design_loop` — so such an
attempt's transition stream starts at `CODE_SYNTHESIS →
BACKTEST_AND_VERIFICATION`. A consumer that derives a drift baseline from the
first transition it sees must handle that case rather than assume boundary 2
is present.

Each transition emits a `PhaseTransition` event (`class PhaseTransition`,
`phases.py`) carrying `from_phase`, `to_phase`, a 64-char SHA-256 `spec_hash`
(`hash_spec` — canonical-JSON of the spec, deliberately excluding
`strategy_code`) and a 64-char SHA-256 `code_hash` (`hash_code`).

This is a drift-detection mechanism, and the key thing to understand is that
**both hashes are boundary snapshots** — each records state as of the moment
its own transition fires, not a value pinned for the rest of the attempt.
Comparing two boundaries detects drift only where none of the carve-outs
below applies.

`spec_hash` is frozen post-design apart from **three** carve-outs. All three
land on the `CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION` boundary, but only
(1) and (2) are inside `_run_synthesis_loop` — (3) fires earlier, in
`_synthesize_initial_code`, before the loop is entered:

1. the refinement loop accepting a **tighten-only** `risk_limits` update
   (`_apply_updates` → `_merge_risk_limits_tighten_only`);
2. a **zero-trade repair** committing a whitelisted `risk_limits` update
   (`_apply_zero_trade_spec_updates`, `../strategy_lab/zero_trade_repair.py`).
   Unlike (1) this path has **no** tighten-only guard — it assigns the
   proposed value verbatim, so a *loosening* is accepted as-is;
3. `_synthesize_initial_code` (`../strategy_lab/orchestrator.py`) flipping
   `spec.requires_custom_code` to `True` on a `CompilerError` the
   mechanical-repair pre-flight didn't catch — and `hash_spec` excludes only
   `strategy_code`, not `requires_custom_code`.

From there `spec_hash` is stable through the terminal transition: the
trade-alignment loop's own `_apply_updates` call never carries `risk_limits`
updates, and `requires_custom_code` isn't touched again after synthesis.

`code_hash` is unchanged from `CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION`
through the refinement loop that precedes it, but the trade-alignment loop
that *follows* it can commit a rewritten baseline
(`_commit_alignment_proposal`), so the terminal transition's `code_hash` can
legitimately differ — the terminal emit is reached with the
`_DesignAttemptState` built after the alignment loop, so it carries that
rewrite. An alignment-driven rewrite is expected behaviour, not drift.

`PhaseTransition`'s own `Invariants` block states the same carve-outs. It
previously asserted flat stability for both hashes — which predated
`_commit_alignment_proposal` and the zero-trade repair path — and was
corrected alongside this document.

All four emission sites live in `orchestrator_design.py` and are the only
`_emit_phase_transition(...)` call sites in the package; in phase order they
are `DESIGN → DESIGN_REVIEW`, `DESIGN_REVIEW → CODE_SYNTHESIS`,
`CODE_SYNTHESIS → BACKTEST_AND_VERIFICATION`, and the terminal
`BACKTEST_AND_VERIFICATION → None`. The mixin that owns the whole-attempt
sequencer emits every transition, even though the phases themselves execute
across all five mixins (below).

## Design ↔ review loop (DESIGN → DESIGN_REVIEW)

Owned by `DesignMixin` (`orchestrator_design.py`). One round branches on
readiness (`_review_and_handle_critique`, `orchestrator_design.py`):

```
DesignAgent.run/.revise → SpecReadinessGate (phase="design")
  → readiness-clean: DesignReviewAgent.run → (ready? done : revise)
  → readiness-critical: synthetic critique from the readiness findings
      (DesignReviewAgent is skipped — no LLM call this round) → revise
```

A critical `SpecReadinessGate` finding never reaches the reviewer: the round
synthesizes a critique from the readiness findings themselves and routes
straight to `DesignAgent.revise`, so only a readiness-clean spec ever costs
a `DesignReviewAgent` LLM call.

- **`DesignAgent`** (`../strategy_lab/agents/design.py`) authors the
  `StrategySpec` only — it never writes code. Before returning, it runs an
  internal **self-review** pass (`STRATEGY_LAB_DESIGN_SELF_REVIEW_ENABLED`,
  default on): a second LLM call audits prose ↔ predicate completeness and
  risk-math coherence, self-revises up to
  `STRATEGY_LAB_DESIGN_SELF_REVISION_ROUNDS` times, and re-audits each
  revision before returning — so a *successful* self-revision that
  introduced a fresh contradiction gets caught before the external reviewer
  ever sees it. This is best-effort, not a hard guarantee: if the final
  self-revision call or its re-audit itself fails, `_with_self_review`
  returns the current spec unchanged and explicitly defers the residual
  issue to the external `DesignReviewAgent` loop, which stays authoritative.
- **Mechanical-repair pre-flight** (`../strategy_lab/mechanical_repair.py`,
  gated by `STRATEGY_LAB_MECHANICAL_REPAIR_ENABLED`) runs before *every*
  review round — fully-determined, semantics-preserving fixes that never
  cost an LLM round. Two stages: unconditionally, coercing an intraday
  timeframe on an asset class with no intraday data and clamping
  `risk_limits.max_position_pct` to the shared ceiling; then, **only on a
  readiness-clean spec** (the `deterministic_ready` guard in
  `orchestrator_design.py`), a trial
  `compile_strategy()` call that promotes a spec to
  `requires_custom_code=True` on `CompilerError` (or, inversely, demotes an
  over-elected custom-code spec back to the compiled path when it turns out
  to compile cleanly — `STRATEGY_LAB_DEMOTE_COMPILABLE_CUSTOM_CODE`). That
  second stage is readiness-gated because the compiler assumes structurally
  valid DSL — a readiness-defective spec can make `compile_strategy` raise a
  *non*-`CompilerError`. Only after mechanical repair exhausts its fixed
  scope does a critical fall through to `DesignAgent.revise`.
- **`SpecReadinessGate`** (`../strategy_lab/quality_gates/spec_readiness.py`) is the
  deterministic implementability check — sizing-coherence math, timeframe
  validity, DSL completeness — invoked at `phase="design"`
  (in `orchestrator_design.py`) and re-checked at synthesis round 0.
- **`DesignReviewAgent`** (`../strategy_lab/agents/design_review.py`) reviews
  the spec plus the readiness gate's findings and emits a `SpecCritique` — it
  never revises the spec or writes code itself, only critiques.
- A `CritiqueLedger` assigns each critique a deterministic, content-derived
  `issue_id` and tracks the open blocking-issue set round over round. The
  loop is capped at `STRATEGY_LAB_DESIGN_REVIEW_ROUNDS` (default 20,
  short-circuits `status="failed: design_not_ready"` on exhaustion) and
  separately short-circuits early as `status="failed: design_stalled"` if the
  open-issue set is non-empty and unchanged for
  `STRATEGY_LAB_DESIGN_REVIEW_STALL_ROUNDS` consecutive rounds (default 3).
  The ledger also flags *regressions* — an issue resolved earlier that
  reappears — as an explicit "do not reintroduce" notice fed back into the
  next revision.
- Across cycles, a `ConvergenceTracker` (`../strategy_lab/quality_gates/convergence_tracker.py`)
  supplies stall/diversity/failure directives that get folded into the design
  prompt, and (when enabled) a once-per-cycle market-regime summary
  (`../strategy_lab/market_regime.py`, `STRATEGY_LAB_REGIME_SUMMARY_ENABLED`) helps the
  designer pick a setup archetype that fits current conditions.

Every numeric cap above is `STRATEGY_LAB_*`-tunable. Defaults are quoted here
only to make the prose concrete —
[`../strategy_lab/README.md`](../strategy_lab/README.md#environment-variables)
is the canonical home for them, and wins on any disagreement; it also carries
what is genuinely not repeated here (worst-case LLM-call sizing, the interplay
between retries and budgets, and the parse/clamp rules).

## Code synthesis (CODE_SYNTHESIS)

`_synthesize_initial_code` (`../strategy_lab/orchestrator.py`) picks one of two paths
per spec:

- **Compiled DSL (default / preferred path)** — `compile_strategy(spec)`
  (`compile_strategy` in `../strategy_lab/synthesis/compiler.py`) deterministically compiles the
  structured `entry_rules`/`exit_rules`/`sizing` DSL into a thin `on_bar`
  shim. All entry/exit decisions are made **engine-side** by
  `_EngineEntryDispatcher`/`_EngineExitDispatcher`
  (`../trading_service/service.py`), so the compiled path
  cannot drift from the spec the way hand-authored code can — it's "faithful
  by construction." Output is byte-identical for a given spec (a SHA-256
  content hash is embedded in the header; no `datetime.now()`/`uuid`/`id()`).
  `compile_strategy` raises `CompilerError` when the spec is outside the
  expressible subset (e.g. `volatility_target` sizing with no matching `atr`
  indicator), which is exactly the signal the mechanical-repair pre-flight
  above uses to promote a spec to the custom-code path *during design*,
  before synthesis ever has to discover it.
- **LLM custom code (`requires_custom_code=True`)** —
  `CodeSynthesisAgent.run(spec)` (`../strategy_lab/agents/code_synthesis.py`)
  authors the strategy's Python directly. This path is guarded by
  `CodeConformanceGate._check_custom_code_faithfulness` (indicator-source
  divergence, falsy guards on an indicator, invalid position attributes) and
  by the multi-layer look-ahead defenses documented in
  [`../strategy_lab/LOOK_AHEAD_DEFENCE.md`](../strategy_lab/LOOK_AHEAD_DEFENCE.md).

The full faithful-execution rationale for preferring the compiled path — and
exactly what guarantees the custom-code path still has to meet — is in
[`../strategy_lab/FAITHFUL_EXECUTION.md`](../strategy_lab/FAITHFUL_EXECUTION.md);
not repeated here.

## Refinement loop (still CODE_SYNTHESIS)

Owned by `SynthesisMixin` (`_run_synthesis_loop` in
`orchestrator_synthesis.py`). Round 0 runs `SpecReadinessGate` at `phase="synthesis"`,
`CodeSafetyChecker`, `CodeConformanceGate`, and `PredicateConformanceGate`
against the just-synthesized code; a `PredicateReachabilityProbe` checks
whether the spec's predicates can actually fire against real market data
before a full backtest is even attempted. On a gate failure or a runtime
exception from executing the code in the sandbox, `RefinementAgent`
(`../strategy_lab/agents/refinement.py`) receives the concrete failure
(syntax error, gate finding, or anomaly) and rewrites the code — the spec
itself does not change here except for the tighten-only `risk_limits`
carve-out. The loop is capped at `STRATEGY_LAB_MAX_CODE_REFINEMENT_ROUNDS`
(default 50) with its own stall detector
(`STRATEGY_LAB_REFINEMENT_STALL_ROUNDS`). The two exits are distinct statuses:
a stall sets `backtest_status="failed: refinement_stalled"`, plain cap
exhaustion sets `"failed: max_refinement_rounds"`. A stall implies exhaustion,
so the stall check runs first and reports the more specific reason.
A **zero-trade backtest** (a strategy that compiles and runs but never
enters a position) is routed to
[`ZeroTradeRepairer`](../strategy_lab/zero_trade_repair.py)/[`ZeroTradeRepairAgent`](../strategy_lab/agents/zero_trade_repair.py)
ahead of the generic refinement path, since "why did this never fire" needs
different diagnosis than a runtime exception — the `coverage_probe/`
subsystem (static + runtime AST instrumentation) supplies the evidence for
that diagnosis.

## Trade-alignment loop (start of BACKTEST_AND_VERIFICATION)

Owned by `AlignmentMixin` (`orchestrator_alignment.py`), after the strategy
has produced a real trade ledger. Each round:

1. **`DeterministicAlignmentChecker`** (`../strategy_lab/quality_gates/alignment_checks.py`)
   evaluates the trade ledger against the structured `StrategySpec`. The
   seven per-rule checks, and which of them can actually drive the
   `aligned`/`misaligned` verdict, are enumerated in
   [`strategy_lab_pipeline.md`](./strategy_lab_pipeline.md) — the canonical
   list, not repeated here. What matters for the loop: the checker is
   deterministic and makes **no LLM call**, so a clean audit costs nothing.
   A near-miss on the entry-signal check (within
   `STRATEGY_LAB_ALIGNMENT_NEAR_MISS_PCT`, resolved by `_near_miss_pct()` in
   `../strategy_lab/quality_gates/alignment_checks.py`) routes to
   `TradeAlignmentAgent.adjudicate_near_miss` — a single-shot yes/no call,
   not a full re-audit.
2. If misaligned, `TradeAlignmentAgent.propose_code_fix` receives the
   structured findings (never raw trade-ledger prose) and returns a rewritten
   strategy file. The proposal is re-checked by `CodeSafetyChecker` at
   `phase="verification"` before being re-executed in the sandbox for a fresh
   backtest, and the loop repeats.

Capped at `STRATEGY_LAB_MAX_ALIGNMENT_ROUNDS` (default 10); reaching the cap
logs a warning and keeps the last-audited trades rather than failing the
cycle. [`strategy_lab_pipeline.md`](./strategy_lab_pipeline.md) covers the same
loop from the outside — the `aligning` phase, its sub-phase events, and the
per-rule check list.

## Verification & publication decision (rest of BACKTEST_AND_VERIFICATION)

Owned by `VerificationMixin` (`orchestrator_verification.py`):

- **Walk-forward acceptance** — `AcceptanceGate.check` (`../strategy_lab/quality_gates/acceptance_gate.py`)
  is a four-criteria check: deflated Sharpe, in-sample→out-of-sample
  degradation, minimum OOS trade count, and a regime-conditional pass
  (beats the configured benchmark in at least `min_regime_beats` of the
  regime subwindows, default 2 of 4) — across the backtest's walk-forward
  folds.
- **Exit-rule conformance** — `ExitRuleConformanceGate`
  (`../strategy_lab/quality_gates/exit_rule_conformance.py`) deterministically checks that
  engine-enforced exits actually matched `spec.exit_rules`.
- **Realism gates** run in fixed order (`_run_realism_gates`,
  `../strategy_lab/orchestrator.py`): `TargetSymbolCoverageGate` (backtest universe
  matches requested symbols), `CostStressRealismGate`, `LiquidityRealismGate`,
  `RegimeCoverageGate`, `TradeClusteringGate`, `RuleFiringRateGate`.

The **winner label** is a simple deterministic threshold —
`is_winning = execution_succeeded and trades and annualized_return_pct >= 8.0`
(`WINNING_THRESHOLD` in `../models.py`) — independent of every gate above. The
**publishable gate** is what actually decides paper-trading eligibility:
`is_publishable = is_winning and realism_passed and trades_aligned and
exit_rule_conformance_passed and not lookahead_violation`. Both the exact
veto-code ordering on a failed publishable gate and the full paper-trading
skip-reason table are already documented in
[`strategy_lab_pipeline.md`](./strategy_lab_pipeline.md) — see "Winner label
vs publishable gate" there rather than duplicating it here.

Analysis (`AnalysisAgent`, `../strategy_lab/agents/analysis.py`) runs last —
a single self-reviewing narrative draft describing why the strategy won or
lost. It does **not** receive the gate-results list: its inputs are the spec,
the metrics (whose `acceptance_reason` carries a summarized veto string), the
trades, the rationale, the `is_winning` verdict, and the alignment report.

## Quality gates catalog

Every deterministic check in `../strategy_lab/quality_gates/` (23 of the
directory's 26 files — see the omission note below), with the phase that
invokes it.

**The Phase column uses `StrategyLabPhase`** (`quality_gates/models.py`:
`"design" | "design_review" | "synthesis" | "verification"`), the tag gates
stamp on their own results — *not* the four-phase `Phase` contract from the
top of this document. They are different enums and the names only partly
overlap: gate-`synthesis` runs inside contract-`CODE_SYNTHESIS`, and
gate-`verification` inside contract-`BACKTEST_AND_VERIFICATION`. Superscripts
mark rows whose invocation needs more than a phase name; those notes follow
the table.

| Gate / file | Phase | Purpose |
|---|---|---|
| `strategy_validator.py`: `StrategySpecValidator` | design, synthesis <sup>a</sup> | Deterministic field-level validation of `StrategySpec` |
| `spec_readiness.py`: `SpecReadinessGate` | design, synthesis <sup>f</sup> | The implementability gate that decides design-loop readiness (sizing coherence, timeframe validity, DSL completeness) |
| `code_safety.py` (+ `code_safety_ast.py`): `CodeSafetyChecker` | synthesis, and verification (alignment-proposal re-check) | AST + regex safety scan of generated strategy Python |
| `code_conformance/gate.py` (+ `ast_helpers.py`): `CodeConformanceGate` | synthesis | Deterministic spec→code conformance, incl. custom-code faithfulness checks |
| `predicate_conformance.py` (+ `predicate_conformance_fixtures.py`, `conformance_bars.py`): `PredicateConformanceGate` | synthesis, re-checked at verification | Pre-execution predicate-conformance shadow check against synthetic bars |
| `predicate_reachability.py`: `PredicateReachabilityProbe` | synthesis | Pre-backtest, data-driven check that spec predicates can actually fire |
| `backtest_anomaly.py`: `BacktestAnomalyDetector` | synthesis, verification <sup>b</sup> | Threshold-based anomaly detection over backtest results |
| `alignment_checks.py`: `DeterministicAlignmentChecker` | verification <sup>e</sup> | The seven per-rule trade-alignment checks described above |
| `acceptance_gate.py`: `AcceptanceGate` | verification | Composite walk-forward acceptance (deflated Sharpe, IS/OOS degradation, OOS trade count, regime-conditional pass) |
| `exit_rule_conformance.py`: `ExitRuleConformanceGate` | verification | Deterministic conformance of engine-enforced exits to `spec.exit_rules` |
| `target_symbol_coverage.py`: `TargetSymbolCoverageGate` | synthesis, verification <sup>c</sup> | Backtest universe matches the requested target symbols |
| `cost_stress_realism.py`: `CostStressRealismGate` | verification | Realism under cost-stress multipliers |
| `realism/liquidity_realism.py`: `LiquidityRealismGate` | verification | Liquidity realism (fill participation vs volume) |
| `realism/regime_coverage.py`: `RegimeCoverageGate` | verification | Coverage across market regimes |
| `realism/trade_clustering.py`: `TradeClusteringGate` | verification | Detects unrealistic trade clustering |
| `realism/rule_firing.py`: `RuleFiringRateGate` | verification | Spec-rule firing-rate realism |
| `convergence_tracker.py`: `ConvergenceTracker` | — <sup>d</sup> | Stall/diversity/failure directives fed into design prompts |
| `universe_injection.py` | — <sup>g</sup> | Deterministic post-synthesis injection of the `UNIVERSE` constant |
| `models.py` | — | Shared `QualityGateResult` / `StrategyLabPhase` types (not a gate itself) |

<sup>a</sup> Two methods, three call sites. `check_hypothesis_rules(spec,
phase="design")` is the hypothesis-vs-rules consistency check feeding
`DesignReviewAgent` (`orchestrator_design.py`). `validate(spec)` runs
pre-synthesis (the pre-synthesis call in `orchestrator_synthesis.py`, defaulting to the same
`phase="design"`), and again on a repaired spec at `zero_trade_repair.py`
tagged `phase="synthesis"`. Readiness is a different gate — `SpecReadinessGate`,
next row.

<sup>b</sup> Four call sites: per refinement round (`orchestrator_synthesis.py`,
`phase="synthesis"`); unconditionally after every trade-alignment round
(`orchestrator_alignment.py`, `phase="verification"`); the walk-forward-failure
fallback recheck with `dsr_aware=False` (`orchestrator_verification.py`); and
inside zero-trade repair (`zero_trade_repair.py`), which passes no `phase=` and
so takes the `"synthesis"` default, emitting `zero_trade_repair_`-prefixed rows.

<sup>c</sup> `check_fetch`/`check_trades` run at synthesis, where a critical
failure fails the run closed before verification; `check_breadth` is a softer
verification-phase check.

<sup>g</sup> Not a gate in the stamping sense: `inject_universe_and_guard`
is a pure AST source rewriter returning `str`. It emits no
`QualityGateResult` and so carries no phase tag, even though it runs during
synthesis.

<sup>e</sup> `check()` runs its whole body inside
`with self._using_phase("verification")`, so every finding it emits is stamped
`phase="verification"` — there is no `"trade-alignment"` tag to grep for,
even though the loop that drives it is the trade-alignment loop.

<sup>f</sup> Stamped `"design"` in the design loop and `"synthesis"` on the
round-0 re-check.

<sup>d</sup> `ConvergenceTracker` stamps no phase — `record()` files an
outcome rather than emitting a `QualityGateResult`. It is called per-attempt
inside `RecordAssemblyMixin`
(`orchestrator_record_assembly.py`) — the same activity scope as
everything else here, not a batch-level step. The directives it *derives* are
what carry across cycles; batch-level merging is a separate step
(`merge_wave_results_activity`).

`quality_gates/__init__.py` and its two subpackage markers
(`code_conformance/__init__.py`, `realism/__init__.py`) are omitted above.

## Record assembly (end of BACKTEST_AND_VERIFICATION)

Owned by `RecordAssemblyMixin` (`orchestrator_record_assembly.py`) — pure
assembly from already-computed state. It makes no *agent* (LLM) calls; its one
gate interaction is `ConvergenceTracker.record(...)`, which files this
attempt's outcome for later cycles rather than evaluating anything.
`_assemble_record` builds the happy-path `StrategyLabRecord`;
`_build_short_circuit_record` builds the equivalent for a cycle that never
reached a full backtest (budget exhausted, design stalled, spec
unimplementable after all re-entries). Module-level `_finalize_loop_telemetry`
merges the design-loop telemetry with whole-funnel gate pass/fail histograms
and derives the three-state `code_path`
(`"not_synthesized" | "compiled" | "custom"`) recorded on the record. The
drift ledgers (`spec_history`, `code_history`, `gate_timeline` — exactly the
three `_DriftCollector` fields) accumulate via the
copy-on-entry/commit-on-completion `_DriftCollector`
(`_orchestrator_helpers.py`) — see
[`../strategy_lab/RETRY_STATE_ISOLATION.md`](../strategy_lab/RETRY_STATE_ISOLATION.md)
for exactly how that isolates one design attempt's drift from the next
attempt's, and from a Temporal activity retry of the same attempt.

## Orchestrator composition

`StrategyLabOrchestrator` composes five mixins by MRO (see
[`architecture.md`](./architecture.md)§11 for the summary table and the link
to [`../strategy_lab/MIXIN_BOUNDARIES.md`](../strategy_lab/MIXIN_BOUNDARIES.md)
for full boundary rationale). The whole-attempt sequence,
`_run_design_attempt` (`orchestrator_design.py`), calls across all
five in order:

```
DesignMixin (design ↔ review)
  → orchestrator._synthesize_initial_code (compiled DSL or CodeSynthesisAgent)
  → DesignMixin._orchestrate_refinement_and_alignment
      → SynthesisMixin (refinement loop)
      → AlignmentMixin (trade-alignment loop)
  → DesignMixin._orchestrate_verification_and_analysis
      → VerificationMixin (walk-forward, realism, publication veto)
      → orchestrator._run_analysis_phase (AnalysisAgent)
  → DesignMixin._extract_findings_and_assemble_record
      → RecordAssemblyMixin (final StrategyLabRecord)
```

## Batch / Temporal activity mapping

Every `@activity.defn` in
[`../strategy_lab/temporal/activities.py`](../strategy_lab/temporal/activities.py),
mapped to what it durability-wraps:

| Activity | Wraps |
|---|---|
| `compute_regime_summary_activity` | Current market-regime summary for the design prompt |
| `resolve_workflow_config_activity` | Resolves every env var the cycle-workflow control flow needs, once |
| `persist_run_state_activity` | Persists strategy-lab run/batch progress |
| `snapshot_prior_records_activity` | Reads the durable record store, sorted by creation time |
| `build_short_circuit_record_activity` | `StrategyLabOrchestrator._build_short_circuit_record` |
| **`run_design_attempt_activity`** | **The entire per-attempt pipeline above, verbatim** (`_run_design_attempt`) |
| `compute_signal_brief_activity` | Signal brief (`SignalIntelligenceExpert`), once per batch — see `architecture.md` §7 for the invocation-count nuance |
| `is_run_cancelled_activity` | Whether the run stopped for an external reason |
| `external_terminal_status_activity` | The run's persisted external stop status, if any |
| `finalize_cycle_record_activity` | Post-`run_cycle` tail: signal-brief attach + paper-trade (`PaperTradingAgent`) + persist |
| `merge_wave_results_activity` | Folds a completed wave's results into the batch-level `ConvergenceTracker` |
| `publish_run_event_activity` | Best-effort SSE publish (fire-and-forget UX side-channel) |

`StrategyLabCycleWorkflow` calls `run_design_attempt_activity` in a
`range(MAX_DESIGN_REENTRIES + 1)` loop — `MAX_DESIGN_REENTRIES` is 2, so up
to **3** design attempts. That *outer* design-re-entry loop is
the only part of this pipeline actually expressed as durable workflow code;
everything from "Design ↔ review loop" through "Record assembly" above runs
inside that one activity call. `StrategyLabBatchWorkflow` calls
`compute_signal_brief_activity` once per batch (see `architecture.md` §7 —
"Temporal-only dispatch" — for the full invocation-count nuance, including
the mid-batch resume case), then fans a wave of cycles out as
`StrategyLabCycleWorkflow` child workflows before awaiting them together,
then calls `finalize_cycle_record_activity` per settled result.

## At a glance

```mermaid
flowchart TB
    subgraph attempt["run_design_attempt_activity — one Temporal activity"]
        direction TB
        D[DesignAgent] --> RG1{SpecReadinessGate<br/>phase=design}
        RG1 -->|readiness-clean| DR[DesignReviewAgent]
        RG1 -->|readiness-critical:<br/>reviewer skipped| D
        DR -->|ready| RC{spec already<br/>requires_custom_code?}
        DR -->|not ready| D
        RC -->|yes: no compile attempt| CSA[CodeSynthesisAgent]
        RC -->|no| CS
        CS{compile_strategy<br/>succeeds?}
        CS -->|yes: compiled DSL| SYN
        CS -->|CompilerError:<br/>requires_custom_code=True| CSA
        CSA --> SYN
        SYN[Pre-execution synthesis gates:<br/>CodeSafety · CodeConformance ·<br/>PredicateConformance · Reachability ·<br/>TargetSymbolCoverage.check_fetch]
        SYN -->|fail| RF[RefinementAgent]
        RF --> SYN
        SYN -->|pass| BT[Execute in sandbox<br/>→ trade ledger]
        BT --> POST[Post-execution, still synthesis phase:<br/>TargetSymbolCoverage.check_trades]
        POST --> EV{critical anomaly<br/>or zero trades?}
        EV -->|zero-trade| ZTRA[ZeroTradeRepairAgent]
        ZTRA --> BT
        EV -->|other anomaly| RF
        EV -->|clean| AL{DeterministicAlignmentChecker}
        AL -->|misaligned| TAA[TradeAlignmentAgent<br/>propose_code_fix]
        TAA --> BT
        AL -->|aligned| VER[AcceptanceGate · ExitRuleConformanceGate ·<br/>6 realism gates]
        VER --> AN[AnalysisAgent]
        AN --> REC[RecordAssemblyMixin<br/>→ StrategyLabRecord]
    end

    SIE[SignalIntelligenceExpert<br/>once per batch] -.->|signal_brief| D
    REC -->|"is_publishable AND<br/>paper_trading_enabled"| PTA[PaperTradingAgent<br/>post-cycle, outside this activity]
```
