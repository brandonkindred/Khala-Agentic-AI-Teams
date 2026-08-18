# Strategy Lab orchestrator: dataclass audit and module boundaries

This note documents the audit of `_orchestrator_helpers.py`'s outcome
dataclasses and the mixin-boundary changes made to right-size the
`StrategyLabOrchestrator` family, and records why the remaining boundaries
were kept as-is. Read this before adding a new cross-mixin dataclass or
glue method — it's the map for "where does this belong."

## Audit: the original 15 dataclasses

| Dataclass | Construct site(s) | Consume site(s) | Cross-file? |
|---|---|---|---|
| `_MarketDataFetch` | `orchestrator.py` `_fetch_market_data` | `orchestrator_synthesis.py` `_fetch_market_data_for_synthesis` | Yes |
| `_VerificationOutcome` | `orchestrator_verification.py` `_run_verification_phase` | `orchestrator_design.py` `_orchestrate_verification_and_analysis` | Yes |
| `_AlignmentLoopOutcome` | `orchestrator_alignment.py` `_run_trade_alignment_loop` | `orchestrator_design.py` (`_orchestrate_refinement_and_alignment`, `_run_design_attempt`) | Yes |
| `_AlignmentRoundOutcome` | `orchestrator_alignment.py` | `orchestrator_alignment.py` only | **No** |
| `_AnomalyRecoveryOutcome` | `orchestrator_synthesis.py` `_handle_critical_anomalies` | `orchestrator_synthesis.py` `_evaluate_synthesis_round` only | **No** |
| `_DesignPersistContext` | `orchestrator_design.py`, `orchestrator_record_assembly.py` (fallback), `temporal/activities.py` (external) | `orchestrator_record_assembly.py` | Yes (+ external) |
| `_DriftCollector` | `orchestrator.py`, several fallback sites, `temporal/activities.py` (external) | Mutated/drained across every mixin | Yes (+ external), stateful collaborator |
| `RefinementStallTracker` | `orchestrator_synthesis.py` | `orchestrator.py` `_refine_or_exhaust` | Yes |
| `_DesignLoopOutcome` | `orchestrator_design.py` `_run_design_loop` | `orchestrator_design.py` `_orchestrate_design_and_review` only | **No** |
| `_DesignPhaseResult` | `orchestrator_design.py` `_orchestrate_design_and_review` | `orchestrator_design.py` `_run_design_attempt` only | **No** |
| `_CodeSynthesisPhaseResult` | `orchestrator.py` `_synthesize_initial_code` | `orchestrator_design.py` `_run_design_attempt` | Yes |
| `_RefinementAlignmentResult` | `orchestrator_design.py` `_orchestrate_refinement_and_alignment` | `orchestrator_design.py` `_run_design_attempt` | Yes (both now in the same file after this refactor; kept shared since it's a genuine phase-boundary envelope, not cluster-internal state) |
| `_SynthesisLoopOutcome` | `orchestrator_synthesis.py` `_run_synthesis_loop` | `orchestrator.py`/`orchestrator_design.py` | Yes |
| `_SynthesisFetchResult` | `orchestrator_synthesis.py` `_fetch_market_data_for_synthesis` | `orchestrator_synthesis.py` `_run_synthesis_loop` only | **No** — and a near-duplicate of `_MarketDataFetch` (same 4 fields + one bool) |
| `_SynthesisEvaluateResult` | `orchestrator_synthesis.py` `_evaluate_synthesis_round` | `orchestrator_synthesis.py` `_run_synthesis_loop` only | **No** |

## What was executed

**1. Merged `_SynthesisFetchResult` into `_MarketDataFetch`.** The two
carried identical `data`/`requested_symbols`/`fetched_symbols`/`provider_used`
fields; `_SynthesisFetchResult` was built by hand-copying those four fields
off the `_MarketDataFetch` its own caller had just received, plus one bool
(`should_break`). Added `should_break: bool = False` to `_MarketDataFetch`
and deleted `_SynthesisFetchResult` outright. This is the one change that
produces a genuine, measurable per-file line-count reduction (removes a
whole dataclass plus its hand-copy boilerplate) rather than relocating
lines elsewhere.

**2. Moved the three cross-cluster glue methods (plus one helper) into
their sole caller's file.** `_orchestrate_refinement_and_alignment`,
`_orchestrate_verification_and_analysis`, `_extract_findings_and_assemble_record`,
and the module-level `_resolve_alignment_report_for_analysis` lived on the
`orchestrator.py` base class specifically because each calls into more than
one mixin. All four, however, have exactly one caller each:
`DesignMixin._run_design_attempt`, which already lives in
`orchestrator_design.py`. Cross-mixin calls resolve at runtime through
`self` regardless of which file physically defines the caller — Python's
MRO doesn't care — so keeping this glue on the base class bought no
import-safety benefit, only an extra file a reader had to open to follow
one design attempt end-to-end. Moved all four verbatim into
`orchestrator_design.py`.

**3. Relocated the 5 single-mixin dataclasses out of `_orchestrator_helpers.py`.**
`_AlignmentRoundOutcome`, `_AnomalyRecoveryOutcome`, `_DesignLoopOutcome`,
`_DesignPhaseResult`, and `_SynthesisEvaluateResult` were never constructed
or consumed outside the one mixin file that used them — they were in the
"shared" helpers module by accident of history, not because anything
shared them. Moved each into its owning file (`orchestrator_design.py`,
`orchestrator_synthesis.py`, `orchestrator_alignment.py`) so that file is
now self-contained for its own outcome types.

### Line counts

| File | Before | After | Delta |
|---|---:|---:|---:|
| `orchestrator.py` | 2060 | 1700 | -360 |
| `orchestrator_design.py` | 1426 | 1886 | +460 |
| `orchestrator_synthesis.py` | 990 | 1059 | +69 |
| `orchestrator_alignment.py` | 740 | 769 | +29 |
| `orchestrator_verification.py` | 531 | 531 | 0 |
| `orchestrator_record_assembly.py` | 414 | 414 | 0 |
| `_orchestrator_helpers.py` | 1288 | 1126 | -162 |
| **Family total** | **7449** | **7485** | **+36** |

The family total moved slightly *up* (+0.5%), not down. Relocating a
method or a dataclass with its docstring intact doesn't shrink the family —
it moves the same lines to a different file, plus each destination needed
its own new import lines for types the source file already had in scope.
The one *deletion* (Step 1's dataclass merge) delivers the acceptance
criterion's "measurable line-count reduction" on its own (`_orchestrator_helpers.py`
and `orchestrator_synthesis.py` both shrink from it); Steps 2 and 3 instead
serve the issue's other stated goal — "understanding or modifying one
pipeline phase requires reading meaningfully fewer files" — by making
`orchestrator_design.py` self-contained for the whole per-attempt flow and
shrinking `_orchestrator_helpers.py`'s shared surface from 15 dataclasses to
9, all of which now cross a real file boundary. `orchestrator.py` itself —
the file the issue's docstring quote singled out as having grown past the
monolith it replaced — shrank by 360 lines (17%).

## Identified but deferred

These were flagged by the audit as conceptually overlapping but were **not**
merged, because doing so would change behavior-carrying contracts rather
than just move code, which is out of this issue's scope:

- **`_AlignmentLoopOutcome` / `_AlignmentRoundOutcome`.** The round outcome's
  core fields (`spec`, `code`, `trades`, `metrics`,
  `ran_on_non_conforming_code`) are a subset of the loop outcome's, but the
  loop outcome also carries cross-round bookkeeping (`alignment_attempts`,
  `alignment_reports`, `trades_aligned`, `rejection_reason`) that has no
  natural value on a single round. Merging would force every round
  construction to either populate or default four fields that don't apply
  at that scope, blurring the "one round vs. the whole loop" distinction
  the code currently uses to reason about loop termination.
- **`_SynthesisEvaluateResult` / `_AnomalyRecoveryOutcome`.** These looked
  like a mechanical duplicate at first read — `_evaluate_synthesis_round`
  does build a `_SynthesisEvaluateResult` by copying fields off an
  `_AnomalyRecoveryOutcome` it just received. But `_SynthesisEvaluateResult`
  is *also* constructed from bare locals on the "clean gates, no anomaly"
  success path, where no `_AnomalyRecoveryOutcome` exists at all. Unifying
  them would mean adding a success/no-anomaly case to
  `_AnomalyRecoveryOutcome` itself — a real contract change to a type
  consumed by `_handle_critical_anomalies`, not a rename.
- **A shared `spec`/`code`/`trades`/`metrics` base dataclass.** This
  4-tuple appeared verbatim in five dataclasses across three files. It was
  a larger, riskier change than this line-count-reduction issue's scope, so
  it was left as its own follow-up — since done: the five dataclasses
  (`_AlignmentLoopOutcome` / `_SynthesisLoopOutcome` in
  `_orchestrator_helpers.py`, `_AnomalyRecoveryOutcome` /
  `_SynthesisEvaluateResult` in `orchestrator_synthesis.py`,
  `_AlignmentRoundOutcome` in `orchestrator_alignment.py`) now inherit the
  4-tuple from a shared `_DesignAttemptState` base defined in
  `_orchestrator_helpers.py`, purely additively — no call site changed.

## Final module boundaries

- **`orchestrator.py`** — `StrategyLabOrchestrator.__init__`, `run_cycle`
  (the pipeline entrypoint), and helpers genuinely used by two or more
  mixins: market-data fetch, benchmark/regime calculations, the generic
  `_refine`/`_refine_or_exhaust` refinement helper, and the backward-compat
  re-export blocks for every extracted module's public symbols.
- **`orchestrator_design.py`** (`DesignMixin`) — the design ↔ review loop,
  plus (as of this refactor) the whole per-design-attempt orchestration:
  `_run_design_attempt` and the three cross-cluster glue methods it alone
  calls, along with their own single-mixin outcome dataclasses
  (`_DesignLoopOutcome`, `_DesignPhaseResult`).
- **`orchestrator_synthesis.py`** (`SynthesisMixin`) — the code-synthesis /
  refinement loop and anomaly recovery, including its own
  `_AnomalyRecoveryOutcome` / `_SynthesisEvaluateResult`.
- **`orchestrator_alignment.py`** (`AlignmentMixin`) — the trade-alignment
  audit/fix loop, including its own `_AlignmentRoundOutcome`.
- **`orchestrator_verification.py`** (`VerificationMixin`) — verification
  and publication-veto decisions. Untouched by this refactor — it had no
  single-file dataclasses and no glue-method involvement.
- **`orchestrator_record_assembly.py`** (`RecordAssemblyMixin`) — building
  the final `StrategyLabRecord`. Also untouched.
- **`_orchestrator_helpers.py`** — now holds the 10 dataclasses that are
  genuinely constructed in one mixin and consumed in another
  (`_MarketDataFetch`, `_VerificationOutcome`, `_DesignAttemptState`,
  `_AlignmentLoopOutcome`, `_DesignPersistContext`, `_DriftCollector`,
  `RefinementStallTracker`, `_CodeSynthesisPhaseResult`,
  `_RefinementAlignmentResult`, `_SynthesisLoopOutcome`), plus the
  dependency-free pure functions every mixin needs and that cannot import
  from `orchestrator.py` or each other. `_DesignAttemptState` is the shared
  `spec`/`code`/`trades`/`metrics` base the dedup item above describes — it
  is constructed directly (not just via its five subclasses) at two call
  sites in `orchestrator_design.py` and two in `orchestrator_synthesis.py`.

Pipeline behavior (design → synthesis → alignment → verification → record
assembly) is unchanged by every step above — each was either a field-for-field
merge (Step 1) or a verbatim relocation (Steps 2-3), verified by running the
full `investment_team` test suite (4835 passed, 10 skipped, ~95.7% coverage,
comfortably above the 90% floor) after each step.
