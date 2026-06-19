---
name: strategy_lab_dsl_stop_limit_exits
overview: Let the Strategy Lab DSL designer author a stop-limit exit (StopLossRule.style="limit") on the structured-exit path. The engine/bracket/custom-code surface already supports STOP_LIMIT; this closes the gap so a rule-authored stop can rest as a structured exit that may legitimately not fill on a gap-through, with fill-based conformance instead of the current fire-based 1:1 reconciliation.
todos:
  - id: dsl-stoplossrule-style
    content: Add style ("market"|"limit", default "market") and limit_offset_pct (Optional[float], gt=0) to StopLossRule in strategy_lab/spec_dsl.py with a model validator requiring limit_offset_pct when style=="limit" and forbidding it when style=="market".
    status: pending
  - id: dsl-format-rule
    content: Render style/limit_offset_pct in _format_rule (spec_dsl.py) so a limit-style stop reads e.g. "stop loss 3% (limit, 0.5% offset)"; keep market-style output byte-identical to today.
    status: pending
  - id: dsl-first-side-stop-factor
    content: Audit first_side_stop_factor / stop_caps_side and the short-safety auto-stop injection in TradingService so a limit-style stop still counts as an effective stop (or is explicitly excluded) — a non-filling stop must not silently disable the worst-case loss bound used for sizing/short-safety.
    status: pending
  - id: exitintent-carries-style
    content: Extend ExitIntent (rule_compiler.py) with style + limit_offset_pct (or a resolved limit basis) so the dispatcher's _build_close_order can branch without re-reading the spec.
    status: pending
  - id: build-close-order-stop-limit
    content: Branch _build_close_order in trading_service/service.py to emit OrderType.STOP_LIMIT (with stop_price/limit_price, TIF=GTC, UnfilledPolicy=REQUEUE_NEXT_BAR) when intent.style=="limit", reusing the bracket limit-price geometry (protective side below stop for a long, above for a short). Keep MARKET for style=="market".
    status: pending
  - id: resting-structured-exit
    content: Introduce a "resting structured exit" in the dispatcher so a limit-style stop is emitted/tracked once per position and latched across bars (let the fill simulator's existing arm/latch/gap-through machinery own it) rather than re-derived and re-emitted every bar; guard against duplicate resting orders for the same position.
    status: pending
  - id: retirement-bindings-live-position
    content: Make _retire_competing_resting_orders and _cancel_pending_entry_continuations NOT treat the position as closed when the emitted exit is a non-filling stop-limit still resting; defer retirement/cancellation to the actual fill so an unfilled stop-limit cannot leave the position live while bindings think it is gone (which would risk an unintended reverse position on a later bar).
    status: pending
  - id: fill-based-firings-counter
    content: Add a fill-based exit counter (e.g. exit_rule_fills / exit_rule_fills_by_symbol) to BacktestExecutionDiagnostics, bumped when an engine exit order actually fills (next to the existing stop_limit_unfilled_triggers handling in _apply_fill_outcome_events), distinct from the emission-time exit_rule_firings bumped in _record_emission.
    status: pending
  - id: conformance-fill-based
    content: Update exit_rule_conformance._check_stop_loss to reconcile below-floor trades against the fill-based counter (not emissions), and tolerate "fired but did not fill" for limit-style stop rules so a legitimate gap-through non-fill does not read as a leak (false critical) and a real leak is not masked.
    status: pending
  - id: prompts-designer
    content: Update strategy_lab/prompts/design_system.md and _stop_order_semantics.md so the designer LLM can author a well-formed limit-style StopLossRule (style + limit_offset_pct), stating the gap-through "may not fill, position stays open" trade-off and when to prefer market vs limit.
    status: pending
  - id: prompts-reviewer
    content: Mirror the stop-limit guidance into the design_review reviewer prompt so the reviewer validates limit-style stops (offset present/sane, gap-through acceptable for the strategy) without false-flagging them.
    status: pending
  - id: tests-dsl
    content: Add spec_dsl tests for StopLossRule style/limit_offset_pct validation (limit requires offset, market forbids it, bounds) and _format_rule rendering for both styles.
    status: pending
  - id: tests-engine-structured-exit
    content: Add structured-exit engine tests mirroring tests/test_stop_limit.py — a DSL limit-style stop fills at limit on a clean cross, does not fill on gap-through (position stays open, counter bumps), fills on recovery while latched, and is retired/discarded correctly when another exit closes the position first.
    status: pending
  - id: tests-conformance
    content: Add exit_rule_conformance gate tests covering a limit-style stop that fired-but-did-not-fill (no critical), reconciliation against the fill-based counter, and that a genuine market-stop leak still criticals.
    status: pending
  - id: tests-coverage-90
    content: Ensure backend line coverage stays >=90% for all touched files (spec_dsl, service, exit_rule_conformance, models, rule_compiler) and the new test modules; justify any pragma:no cover inline.
    status: pending
isProject: false
---

## Background

PR #921 added `STOP_LIMIT` as a real, executable order type, but only on the
**engine / bracket-attachment / custom-code** surface. The DSL *designer* still
cannot author a stop-limit exit. Today a strategy author who needs one must set
`requires_custom_code: true` or use a `StopAttachment(limit_offset=...)` bracket
leg. This plan exposes a stop-limit on the structured-exit (rule-authored) path.

The hard part is that the structured-exit path is built on a guarantee the
bracket path does not make: **a structured exit always closes the position**.
A stop-limit can legitimately *not* fill on a gap-through, which collides with
three assumptions baked into the structured-exit code:

1. `trading_service/service.py` `_build_close_order` always emits
   `OrderType.MARKET` (a guaranteed next-bar-open fill).
2. `strategy_lab/quality_gates/exit_rule_conformance.py` `_check_stop_loss`
   reconciles below-floor trades against engine *firings* (emissions) **1:1** —
   a fire with no matching closed trade reads as a leak (false critical), or
   masks a real one.
3. The position-retirement bindings (`_retire_competing_resting_orders`,
   `_cancel_pending_entry_continuations`) assume the position is closed the
   moment an engine exit emits. A non-filling DSL stop-limit would leave the
   position live while those bindings think it is gone — risking an unintended
   reverse position on a later bar.

## What already exists (reuse, don't rebuild)

The engine STOP_LIMIT machinery from PR #921 is complete and should be reused:

- `engine/execution_model.py` — `stop_limit_triggered` (trigger test) and
  `stop_limit_reference_price` (returns the limit price when filled, `None` on
  gap-through).
- `engine/fill_simulator.py` (~264–334) — arms a STOP_LIMIT on first stop cross,
  latches it into a resting limit, binds it to its position, and emits a
  `FillDiagnosticEvent(kind="stop_limit_unfilled", reason="stop_limit_gap_through")`
  when the bar gaps through.
- `engine/fill_simulator.py` (~1509–1640) — bracket-child limit-price geometry
  for `StopAttachment(limit_offset=..., limit_offset_kind=...)`; the protective
  limit sits below the stop for a long (sell) and above for a short (buy). The
  DSL `_build_close_order` branch should reuse this same geometry.
- `models.py` (~609–657) — `BacktestExecutionDiagnostics` already carries
  `exit_rule_firings`, `exit_rule_firings_by_symbol`, `exit_rule_firings_by_basis`,
  and `stop_limit_unfilled_triggers`.
- `service.py` `_apply_fill_outcome_events` already bumps
  `stop_limit_unfilled_triggers` off the diagnostic event.
- `quality_gates/exit_rule_conformance.py` already surfaces
  `stop_limit_unfilled_triggers` as additive telemetry — the new fill-based
  reconciliation builds on that framing.

So the DSL work is mostly: a new DSL surface, an emission branch that produces a
*resting* structured stop-limit (instead of a fresh market close each bar), a
fill-based counter, conformance that reconciles against fills, retirement
bindings that wait for the fill, and prompts/tests.

## Goals

- A rule author can write a stop-limit exit:
  `StopLossRule(pct=..., style="limit", limit_offset_pct=...)`.
- `style="market"` (the default) is byte-for-byte unchanged — same DSL output,
  same MARKET emission, same conformance behavior, same retirement timing.
- A limit-style structured stop **rests and latches per position** across bars
  using the existing engine machinery, rather than being re-derived/re-emitted.
- A legitimate gap-through non-fill is **not** a conformance leak, and a real
  leak is still caught — because conformance now reconciles against *fills*.
- Retirement/cancellation of competing orders waits for the actual fill, so a
  resting unfilled stop-limit never strands a live position behind bindings that
  think it closed.
- Designer and reviewer LLMs author and validate limit-style stops correctly.

## Non-goals

- No change to the bracket-attachment or custom-code STOP_LIMIT surfaces (PR #921).
- No change to take-profit, signal-exit, or trailing-stop semantics beyond what
  is needed to keep `first_side_stop_factor` / short-safety correct.
- No new execution model; reuse `RealisticExecutionModel` / `OptimisticExecutionModel`.

## Design notes / decisions to confirm during implementation

- **Limit basis.** The issue offers `limit_offset_pct` or a "limit basis".
  Recommend `limit_offset_pct` relative to the stop price (mirrors the bracket
  `limit_offset_kind="bps"` math, just expressed as a pct), keeping a single
  mental model with the existing bracket geometry.
- **Resting vs re-emit.** The dispatcher currently re-evaluates exit rules every
  bar and emits a fresh close. For a limit-style stop we must emit/track once per
  position and let it rest. Decide where the per-position "already has a resting
  structured stop-limit" flag lives (on `_TrackedPosition`) and how it is cleared
  when the position closes or the order is discarded by the stale-continuation
  guard.
- **Effective-stop accounting.** A non-filling stop still bounds intended loss but
  not realized loss. Confirm whether `first_side_stop_factor` should keep treating
  a limit-style stop as the worst-case bound (it caps *intent*) for sizing and the
  short-safety auto-stop, and document the choice in the docstring.
- **Counter naming.** Keep emission-time `exit_rule_firings` (used by the
  rule-firing-rate gate) and add a parallel fill-based counter; do not repurpose
  the existing one, or other gates that read firings will shift.

## Affected files

- `backend/agents/investment_team/strategy_lab/spec_dsl.py` — DSL surface + `_format_rule`.
- `backend/agents/investment_team/trading_service/service.py` — `_build_close_order`,
  resting structured exit, retirement bindings, fill-based counter bump.
- `backend/agents/investment_team/trading_service/strategy/rule_compiler.py` — `ExitIntent`.
- `backend/agents/investment_team/models.py` — `BacktestExecutionDiagnostics` fill counter.
- `backend/agents/investment_team/strategy_lab/quality_gates/exit_rule_conformance.py` — `_check_stop_loss`.
- `backend/agents/investment_team/strategy_lab/prompts/design_system.md`,
  `.../prompts/_stop_order_semantics.md`, and the `design_review` reviewer prompt.
- `backend/agents/investment_team/tests/` — `test_stop_limit.py` mirror for the
  structured path, `test_exit_rule_conformance_gate.py` additions, spec_dsl tests.

## Risks

- **False conformance flips.** Getting the fire-vs-fill distinction wrong will
  either green-light real leaks or red-flag legitimate gap-throughs. The gate
  tests must cover both directions explicitly.
- **Reverse-position bug.** If retirement bindings retire competing orders while
  a stop-limit is still resting unfilled, a later bar can open an unintended
  reverse position. This is the highest-severity correctness risk; cover it with
  a dedicated engine test ("resting unfilled stop-limit does not strand the
  position / does not reverse").
- **Default-path regression.** `style="market"` must be untouched end to end.
  Keep the market branch first and assert byte-identical DSL output and MARKET
  emission in tests.

## Validation

- `cd backend && make lint && make test` (investment_team suite), coverage >= 90%
  on touched files.
- New/updated tests for: DSL validation + rendering, structured-exit fill /
  gap-through / recovery / discard, retirement-binding correctness with a resting
  unfilled stop-limit, and fill-based conformance (no false critical, real leak
  still caught).
