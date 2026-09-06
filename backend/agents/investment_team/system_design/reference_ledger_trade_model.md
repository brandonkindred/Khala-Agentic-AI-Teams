# Reference Ledger — Trade Data Model

This doc designs the trade record shape and module boundary for an
independent, pure reference-ledger simulator: a second, standalone
implementation of a spec's entry/exit decision logic that a later
trade-matching module can diff against the production ledger
(`TradeRecord`, documented in [`trade_record_schema.md`](./trade_record_schema.md))
to catch drift between what a spec says and what the live engine actually
does. This document is a design only — it specifies the record schema, the
module's public interface, and per-exit-rule fill semantics precisely enough
for a later implementation step to build directly against it. No simulator
code exists yet.

## 1. Purpose & scope

The reference ledger answers one question: *given a spec's entry/exit rules
and a fixed sequence of bars, what trades should a faithful, side-effect-free
re-implementation of the decision logic produce?* It exists to be diffed
against the production engine's actual trade output, so a mismatch signals
either a spec-compilation bug or an engine-fidelity regression.

**A note on current vs. target production behavior:** at the time of this
writing, **both** OCO bracket legs — the take-profit leg and the stop leg
*regardless of its `style`* — plus any standalone `style="limit"` stop
already rest at their authored price in production. The bracket stop is
worth stating explicitly because `BracketStopLeg.style` defaults to
`"market"`, which might suggest bar-close detection: it does not.
`_materialize_bracket_children` submits that leg as a resting
`OrderType.STOP` child on entry fill (a `style="limit"` leg becomes
`STOP_LIMIT`, a trailing one `TRAILING_STOP`), so **no** bracket leg goes
through the bar-close path and **no** interim suppression applies to
either of them. What still uses the old approximation is exactly the
standalone `stop_loss`/`take_profit`/`scaled_take_profit` rules with
`style="market"`: they fire via bar-close detection and close at the
*next* bar's market open, the same next-bar-open approximation
`signal_exit` uses. This document
deliberately models the *target* resting-order behavior a separate,
in-flight execution-fidelity change will ship for those standalone rules
(exact level, or worse-of-open-and-level on a gap, filled on the trigger bar
itself) — because once that behavior ships, comparing the reference ledger
against the pre-change approximation would make every stop/take-profit trade
trivially "diverge." Implementers should not be surprised that this document
does not match the current `_build_close_order` behavior for those three
rule kinds; that divergence is intentional and temporary. Until the
execution-fidelity change ships, the later trade-matching module must read
a **fill-mechanics** divergence for `stop_loss`/`take_profit`/
`scaled_take_profit` during this interim window the same way it reads a
participation-cap divergence below — as outside this module's modeled
(target) behavior, not a spec/engine mismatch to flag. This suppression is
narrower than "every divergence," though: it covers a trade whose
*identity* matches (same rule kind, direction, decision bar, and relative
sequence) but whose fill price/fill bar differ only because of the
next-bar-open approximation — not a **structural** divergence, such as a
trade present in one ledger but absent from the other, or a
`scaled_take_profit` position that fires a different number of rungs
between the two. A structural divergence during the interim window is
still a genuine spec/engine mismatch this reference ledger exists to catch
and must not be suppressed alongside the expected fill-mechanics noise.

**The "same decision bar" clause needs two carve-outs**, because this
document's own target-state rules deliberately move the decision bar
relative to current production in two places. Without them, the identity
key never matches for these cases, the suppression never engages, and the
matching module reports expected interim noise as a real mismatch:

- **An entry-bar exit on a market entry.** Production's exit dispatcher
  sets `just_opened = pos.entry_order_type != "market"`, so a *market*
  entry leaves `just_opened` `False` and an `entry_price` stop or
  take-profit can fire on the position's **own entry bar**, from that
  bar's `high`/`low` against `entry_price`. This module defers every
  resting order to `entry_bar + 1` (§5's "Per-bar evaluation order"), so
  its decision bar for that trade is at least one bar later — and its
  `entry_bar < exit_bar` invariant (§3) makes an entry-bar exit
  unrepresentable by construction. During the interim window the matching
  module must therefore accept a production trade whose decision bar is
  `entry_bar` against a reference trade whose decision bar is later, for
  the same rule kind and direction, as fill-mechanics noise.
- **A ladder rung deferred by the current-bar reachability gate.**
  Production's `_next_scaled_rung` is purely high-water-mark based: once
  the watermark since entry has reached a rung's target, that rung stays
  eligible on every later bar even after price retraces. This module adds
  a current-bar reachability gate on top (§5's `scaled_take_profit`
  subsection), so a rung production fires on a retraced bar is deferred
  here until some bar actually trades at the level. Rung *bars* can
  therefore differ, which shifts the aggregated `exit_price` and possibly
  which rule performs the final close. Treat a rung-bar difference of this
  shape as fill-mechanics noise too — but note the boundary: a *different
  number of rungs firing overall* remains structural, per the paragraph
  above, since that changes the position's realized shape rather than just
  when a rung landed.

It is **not** a fill-cost engine. Explicitly out of scope:

- **Slippage and transaction costs.** Reference prices are exact bar-derived
  levels (a resting order's authored price, or worse-of-open-and-level on a
  gap), not slippage-adjusted fills — `ReferenceTrade.entry_price`/
  `exit_price` are never a raw slippage adjustment applied post hoc to an
  otherwise-fixed price. See §3 for exactly which production field this
  corresponds to. `entry_slippage_bps` is not therefore inert: it still
  shapes internal bookkeeping (the `entry_price_basis` anchor used by
  `basis="entry_price"` exits and the trailing-stop watermark seed, and the
  internal post-slippage capital/equity ledger used for sizing and
  capital-sufficiency gating), which can in turn change which price and bar
  a `basis="entry_price"` exit records, and which trades this module emits
  at all via the capital-gating channel. §2's `entry_slippage_bps` parameter
  description is the single authoritative enumeration of both uses and
  their downstream effects; this bullet only establishes the scope
  boundary, not the mechanism.
- **Order-book / partial-fill mechanics.** No order queue, no
  participation-cap clipping, no multi-slice fills. One trigger, one fill,
  at a single price. This is a real, deliberate simplification, not an
  oversight: production's default execution model **does** clip an
  over-sized order to a participation-cap fraction of the bar's dollar
  volume and can requeue the remainder across later bars — this module
  does not model that. Consequently, a divergence this reference ledger
  reports for a low-liquidity symbol or an outsized order (relative to bar
  volume) should be read as "outside this module's modeled execution
  mechanics," not a rule-evaluation bug the later matching module should
  flag the same way it would a genuine spec/engine mismatch. The same
  exclusion covers a second, related mechanic for the same reason: the
  default `RealisticExecutionModel` layers a participation-dependent
  **adverse-selection slippage haircut** (`extra_slip_bps`, on top of the
  base `slippage_bps`) onto `LIMIT`/`STOP_LIMIT` fills specifically —
  `take_profit`, `scaled_take_profit` rungs, `bracket_take_profit`, and a
  `style="limit"` `stop_loss`/`bracket_stop_loss` all materialize as one of
  those two order types in production. This module's internal
  `entry_price_basis`/`exit_price_basis` capital-ledger formulas (§5's
  `stop_loss` and "Entries" subsections) use only the base
  `entry_slippage_bps` input and do not, and cannot, reproduce this haircut
  — it is participation- and next-bar-dependent, computed inside
  `execution_model.py`, which §2 already forbids this module from
  importing. Consequently, this module's tracked capital after a
  `take_profit`/`scaled_take_profit`/limit-style-stop exit can be higher
  than production's real (haircut-reduced) capital, and a later entry this
  module's capital-sufficiency check admits on that basis, while
  production's real run rejects it for insufficient capital, is an expected
  divergence attributable to this same excluded mechanic — not a
  rule-attribution bug.
- **Cost-aware position sizing.** Entry quantity is resolved from
  `spec.sizing` against a running equity figure this module tracks itself
  (seeded from the `starting_equity` input; see §5's "Entries" subsection).
  This figure is **not** an idealized no-slippage abstraction — production
  has no such concept either: `_compute_qty`'s own `equity =
  portfolio.mark_to_market()` is `Portfolio.capital` (already post-slippage,
  §5's "Fill-time capital sufficiency") plus unrealized mark-to-market value
  (post-slippage on the short side, via `entry_price_basis`, since
  `Position.entry_price` there is itself post-slippage; no entry-price term
  at all on the long side, so no slippage concept applies there in either
  production or this module). This module's equity is genuinely
  cost-inclusive to that same extent — the divergence from a true backtest
  is narrower than "no-slippage": limited to the mechanics §1's other
  bullets already exclude (order-book/participation effects, and the
  execution-model adverse-selection haircut on `LIMIT`/`STOP_LIMIT` fills),
  not slippage in general. A ladder rung's own quantity is
  whatever its `qty_fraction * original_qty` implies once the entry quantity
  is known.

## 2. Module boundary

```python
def simulate(
    spec: StrategySpec,
    bars: Mapping[str, Sequence[Bar]],
    starting_equity: float,
    entry_slippage_bps: float,
) -> List[ReferenceTrade]:
    """Pure re-simulation of spec.entry_rules / spec.exit_rules over bars.

    Preconditions:
        - spec is a validated StrategySpec, with spec.requires_custom_code
          False.
        - bars contains a non-empty, strictly timestamp-increasing Bar
          sequence for every symbol spec references, and every Bar in
          bars[key] has bar.symbol == key (the mapping key is the sole
          source of symbol identity this module gates and attributes
          output against; see §2's Contract for the full statement).
        - starting_equity is finite and > 0 (math.isfinite; +inf equity
          sizes an infinite quantity that passes the ReferenceTrade
          qty > 0 invariant undetected, while NaN equity propagates NaN
          through sizing and capital arithmetic and fails every entry's
          qty > 0 gate as a late, opaque invariant violation rather than
          a clear input error -- both are excluded by this precondition,
          for different reasons).
        - entry_slippage_bps is finite and 0 <= entry_slippage_bps < 10_000
          (math.isfinite; an infinite value would make entry_price_basis
          infinite or -infinite and propagate NaN into downstream capital
          arithmetic, and at >= 10_000 the exit-side basis
          `price * (1 - bps / 10_000)` goes zero or negative — yielding
          non-positive fill levels for basis="entry_price" rules and a
          sign-inverted cash ledger. Production's own
          BacktestConfig.slippage_bps is `Field(default=2.0, ge=0)`, with
          no upper bound, so this bound is this module's to enforce, not a
          guarantee it can inherit).

    Returns:
        One ReferenceTrade per fully closed position, in global emission
        order (§2's Contract spells out both in full).
    """
```

- `spec` is the existing `StrategySpec` Pydantic model
  (`agents/investment_team/models.py`) — reused verbatim, no
  translation layer.
- `bars` is keyed by symbol, each value an existing `Bar` sequence
  (`trading_service/strategy/contract.py`) — keyed because `StrategySpec`
  can target more than one symbol and every existing decision structure
  (`PositionState`, `ExitIntent`) is already symbol-scoped. A single-symbol
  spec simply passes a one-entry mapping.
- `starting_equity` seeds the equity figure entry-quantity sizing resolves
  against (§5's "Entries" subsection). It is a required third parameter, not
  read off `spec`: `StrategySpec` carries no capital field — starting
  capital lives on the separate `BacktestConfig.initial_capital`, a model
  paired with a spec only at the backtest-orchestration layer, not part of
  `StrategySpec` itself. A caller reproducing a specific backtest run passes
  that run's `BacktestConfig.initial_capital` through as `starting_equity`.
- `entry_slippage_bps` — likewise not read off `spec`: it mirrors the
  separate `BacktestConfig.slippage_bps`. It has exactly two internal uses:
  (1) computing an internal entry-price *basis* (`entry_price_basis`) for
  anchoring `basis="entry_price"` stop-loss/take-profit/scaled-take-profit
  levels and the trailing-stop watermark seed (§5's `stop_loss`
  subsection) — production's real engine anchors those against
  `Position.entry_price`, which is the **post-slippage** fill, not the
  pre-slippage bid `ReferenceTrade.entry_price` reports (§3); and (2) the
  internal **capital** (cash) ledger's post-slippage entry/exit accounting
  (§5's "Entries" subsection, "Fill-time capital sufficiency"), which uses
  the same `entry_price_basis` and its exit-side counterpart to debit/credit
  cash. `ReferenceTrade.entry_price` itself always stays the pre-slippage
  bid regardless of this parameter, but `exit_price`/`exit_bar` are **not**
  fully insulated from it: a `basis="entry_price"` exit's level is
  `entry_price_basis * (1 ± pct)`, and that computed level is the exact
  price the position fills at — becoming `ReferenceTrade.exit_price`
  directly — so changing `entry_slippage_bps` can change which price (and
  which bar) such an exit records. This does **not** mean the module
  applies a separate, post-hoc slippage adjustment to an otherwise-fixed
  exit price: the *character* of every emitted price never changes — each
  is either a bar-derived reference level, or a rule-computed level that
  for `basis="entry_price"` exits legitimately carries entry slippage
  through `entry_price_basis` (as just described), never a slippage
  adjustment layered on afterward. Capital can also gate whether a
  *later* entry is admitted, so this parameter affects which trades
  this module emits through that channel too. A caller reproducing a
  specific backtest run passes that run's `BacktestConfig.slippage_bps`
  through.
- Return value: one `ReferenceTrade` per **fully closed** position, in
  emission order — never one row per partial exit. A position reduced by one
  or more `scaled_take_profit` rungs before its final closing event
  aggregates into a single row (§5's "Exit aggregation" subsection), the
  same way production only builds a `TradeRecord` once `pos.is_closed`. A
  position still open when `bars[symbol]` runs out produces **no** row at
  all — mirroring production, which reports it via
  `TradingServiceResult.open_position_entry_reasons` instead of a
  `TradeRecord`, not as a synthetic force-close.

### Cross-symbol processing order

For a multi-symbol spec, `simulate` must walk `bars` as a single merged,
chronological timeline, not process each symbol's sequence independently —
entry sizing and equity tracking depend on the state of every symbol's
position as of a given point in time, not just the symbol currently being
evaluated. The merge orders bar events by `(timestamp, symbol)`, the same
tie-break `HistoricalReplayStream.__iter__` uses for same-timestamp bars
across symbols. Each symbol's own `entry_bar`/`exit_bar` indices (§3) remain
indices into that symbol's own `bars[symbol]` sequence — the global timeline
is a processing-order concern only, not part of the `ReferenceTrade` schema.

### Reuse

The module must reuse the existing pure rule-decision evaluators rather than
re-deriving *whether* a rule fires:

- `strategy_lab/executor/predicate_evaluator.py::evaluate_entry_rules`,
  `evaluate_signal_exit_rules` — entry and signal-exit trigger decisions.
- `strategy_lab/executor/rule_compiler.py::evaluate_exit_rules_for_position`
  / `first_exit_intent_for_position`, and its `PositionState`, `BarSnapshot`,
  `ExitIntent`, `ExitRuleKind` types — stop/take-profit/scaled-take-profit
  trigger decisions and rule-priority resolution.

What these evaluators do **not** cover, and what this module's simulate loop
must newly implement, is: resting-order *fill-price* mechanics (gap handling
— worse-of-open-and-level, trailing-stop watermark ratcheting, ladder-rung
sequencing across bars, and stop-limit arm/latch behavior); turning a
matched entry signal into an actual fill (bar and price); and entry-quantity
resolution (§5's "Entries" subsection covers both of the latter two). This
logic lives inside the production fill engine and dispatchers, which this
module must not depend on (see below) — so it is modeled here at the
semantic level described in §5, as new pure code, not imported from the
production engine.

One piece of this — the trailing-stop watermark ratchet specifically — is
not unique to the production engine: `strategy_lab/quality_gates/
exit_rule_conformance.py::_check_stop_loss_trailing_replay` already
implements a pure, engine-independent bar-by-bar watermark reconstruction
(seeding the running high/low at entry, evaluate-then-extend per bar,
calling the shared `rule_compiler.stop_loss_triggers` geometry this design
already mandates reusing) as an opt-in conformance replay. It is not a
candidate for wholesale import here: its module carries a top-level import
of `trading_service/service.py` (for `ENGINE_EXIT_REASON_PREFIX`), which
this design's Exclusions below forbid. This module's own trailing-watermark
implementation is therefore still new code, not a reuse of that gate — but
because a second, independent implementation of the same watermark-ratchet
semantics already exists, this module's version must be pinned to it by a
parity test (comparing ratchet/trigger-bar outputs on shared fixture bars),
the same discipline §2's `PandasHistoryView` fallback below already requires
for a duplicated indicator view.

### Exclusions

This module must not import, directly or transitively:

- `trading_service/service.py` (the live dispatchers and their engine-exit
  reason-string constant),
- `trading_service/engine/fill_simulator.py` (the fill-money-math and
  scale-out sequencing engine that consumes the order book below),
- `trading_service/engine/order_book.py` (`PendingOrder`/`OrderBook` — the
  pending-order state machine itself: `FILL_QTY_REL_TOL`, the per-symbol
  FIFO walk, trailing-stop watermark state, the stop-limit arm/latch flag,
  bracket-child materialization, and OCO sibling cancellation),
- `trading_service/engine/execution_model.py` (slippage/reference-price
  derivation),
- `trading_service/engine/portfolio.py` (the live position/portfolio state
  carrier).

The one dependency this design leaves open for the implementation step to
confirm rather than resolve here: a `signal_exit` rule's predicate can
reference indicators, so `simulate()` needs some form of history/indicator
view over the raw `bars` it receives. `predicate_evaluator.py`'s
`PandasHistoryView` is the natural candidate — it already exists independent
of the live engine — but the implementation step should confirm it carries
no import chain back into the excluded modules above before relying on it.
This does not weaken the Postconditions' no-forbidden-import guarantee below
into a contingency: `PandasHistoryView` is defined in the same
`predicate_evaluator.py` module the Reuse list above already mandates
importing `evaluate_entry_rules`/`evaluate_signal_exit_rules` from, so the
fallback below applies only to one of two distinct cases:

- **A forbidden chain local to `PandasHistoryView`'s own code** (e.g., a
  function-level import inside one of its methods, not touched by importing
  the module's other names): the implementation must **not** relax the
  exclusion list to accommodate it — it must instead construct a narrow,
  local read-only history/indicator view over `bars` (reusing only
  `PandasHistoryView`'s indicator-computation logic, not its import) and
  pass that to `evaluate_signal_exit_rules`. Since duplicating indicator
  logic instead of importing it creates its own silent-divergence risk for a
  reference oracle that must match production's signal-exit decisions, any
  such local view must be pinned to `PandasHistoryView`'s behavior by a
  parity test (comparing indicator outputs on shared fixture bars) before
  this module relies on it.
- **A forbidden chain at `predicate_evaluator.py`'s own module level** (e.g.,
  a top-level import in that file): the local-view fallback above does not
  help, since merely importing the Reuse-mandated
  `evaluate_entry_rules`/`evaluate_signal_exit_rules` would already violate
  the exclusion Postcondition, independent of `PandasHistoryView`. This
  would mean the Reuse mandate and the Exclusions Postcondition directly
  conflict — not a case Step 2 should silently work around by re-deriving
  the evaluators; the implementer must stop and escalate the conflict
  instead. (Verified against the current source: `predicate_evaluator.py`'s
  own top-level imports are only stdlib, `pandas`, and sibling
  `strategy_lab` modules — no *direct* forbidden import as of this writing.
  A transitive chain through those sibling imports' own top-level imports
  is **not** ruled out by this check alone; Step 2 must confirm the full
  transitive import closure carries no chain back into the excluded
  modules before relying on it, exactly as the operative instruction above
  already requires.)

In the first case, the Postconditions' no-forbidden-import guarantee holds
regardless of which concrete history-view implementation Step 2 picks; the
second case is a design-level conflict outside Step 2's authority to
resolve unilaterally.

### Contract

**Preconditions:**
- `spec` is a validated `StrategySpec` (Pydantic's own validators already
  enforce its internal invariants — e.g. at most one `OcoBracketRule`,
  strictly increasing ladder `pct` values, and `sum(qty_fraction) <= 1.0 +
  LADDER_SUM_TOL` across a `ScaledTakeProfitRule`'s levels, **not** an exact
  `<= 1.0` bound — the validator's own tolerance permits a sum up to
  `1.0 + LADDER_SUM_TOL` to absorb float-summation noise. `simulate` must
  **not** assume the rungs' fixed `qty_fraction * original_qty` slices never
  over-close a position on their own; see §5's `scaled_take_profit`
  subsection for the remaining-position clip this requires).
- `spec.requires_custom_code is False`. A `requires_custom_code=True` spec's
  production entries/quantities come entirely from LLM-authored
  `strategy_code`, not `spec.entry_rules`/`spec.sizing` — the backtest mode
  passes `entry_rules=None`, `sizing=None`, `target_symbols=None` to the live
  dispatcher for such a spec, bypassing the DSL engine path this module
  re-simulates entirely. Re-simulating `spec.entry_rules` against a
  custom-code spec would produce a ledger with no relationship to what
  production actually traded — this module is out of scope for that case
  (already covered by a separate replay-oracle epic for Path-B/custom-code
  faithfulness).
- For every symbol `spec` references, `bars[symbol]` is non-empty and
  strictly increasing by `timestamp`, and every `Bar` in `bars[key]`
  satisfies `bar.symbol == key` — the mapping key, not each `Bar`'s own
  `symbol` field, is the sole identity this module gates entry rules
  against (§5's "Target-symbol gating") and attributes `ReferenceTrade`
  output to; a mismatched `bar.symbol` would make that attribution
  ambiguous and is out of scope for this module to detect or reconcile.
- `starting_equity` is finite and `> 0` (`math.isfinite` — `inf` starting
  equity yields an infinite quantity that passes `ReferenceTrade.qty > 0`
  undetected; `NaN` yields a quantity that fails the same check only as a
  late, opaque invariant violation rather than a clear input error — both
  are excluded by this precondition, for different reasons).
- `entry_slippage_bps` is finite and `0 <= entry_slippage_bps < 10_000`
  (`math.isfinite` — an infinite value would make `entry_price_basis`
  infinite/`-inf` and propagate `NaN` into downstream capital arithmetic).
  The **upper** bound is what keeps the basis positive: the exit-side
  adjustment is `price * (1 - entry_slippage_bps / 10_000)`, so at exactly
  `10_000` bps it collapses to zero and above that goes negative —
  producing non-positive `basis="entry_price"` exit levels recorded
  directly as `ReferenceTrade.exit_price` (violating §3's `exit_price > 0`
  invariant only *after* the fact, deep in the run) and flipping the
  capital ledger's entry debit into a credit. That is exactly the "late,
  opaque invariant violation rather than a clear input error" failure class
  this precondition block excludes `+inf`/`NaN` equity for, so it is
  excluded here too. This bound is **this module's to enforce**, not one it
  can inherit: production's `BacktestConfig.slippage_bps` is
  `Field(default=2.0, ge=0)` — no upper bound at all, so a misconfigured
  value (a `20000` typo) is accepted upstream and must be rejected here.

**Postconditions:**
- The returned list is in **global emission order**: a `ReferenceTrade` is
  appended to the output at the point its position's *final closing event*
  is reached while walking the merged `(timestamp, symbol)` timeline (§2's
  "Cross-symbol processing order") — not at the point it opened. Trades
  from different symbols interleave in the overall list according to when
  each one's position actually closes, not grouped by symbol. Consequently,
  the subsequence of trades belonging to any single symbol is ordered by
  non-decreasing `entry_bar` (equivalently `exit_bar`): the entry-suppression
  rule in §5's "Entries" subsection means one symbol never holds two
  overlapping positions, so that symbol's trades close in the same relative
  order they opened.
- Every `ReferenceTrade` satisfies `0 <= entry_bar < exit_bar <
  len(bars[symbol])` — strict, not `<=`: no modeled exit kind can complete
  on `entry_bar` itself (every resting-order kind is first eligible at
  `entry_bar + 1`; `signal_exit` may trigger on `entry_bar` but always
  fills one bar later), so `entry_bar == exit_bar` cannot occur.
- Each fully closed position produces exactly one `ReferenceTrade` — a
  position reduced by prior `scaled_take_profit` rungs aggregates them into
  a single row (§5's "Exit aggregation" subsection) rather than emitting one
  per rung.
- A position still open at `bars[symbol]`'s last bar produces no
  `ReferenceTrade` for that position.
- The module implementing `simulate` imports no module listed in §2
  Exclusions, directly or transitively — the same scope as §2's own "must
  not import" rule (module-level, not merely "as a result of calling
  `simulate`": a top-level `import` of an excluded module at the top of the
  implementing file would violate this even though it technically executes
  before any call to `simulate`).

**Invariants:**
- `simulate` has no side effects: it does not mutate `spec` or `bars`, and
  performs no I/O.
- `simulate` is deterministic — identical
  `(spec, bars, starting_equity, entry_slippage_bps)` inputs always produce
  an identical output list. This is required for it to function as a
  reference oracle; a non-deterministic simulator cannot be diffed
  meaningfully against a single production run. `starting_equity`'s role is
  confined to sizing/capital (it never touches `entry_price`/`exit_price`
  directly); `entry_slippage_bps`'s role is **not** as narrow — beyond
  sizing/capital, it also reaches `exit_price`/`exit_bar` indirectly for
  any `basis="entry_price"` exit — see §2's `entry_slippage_bps` parameter
  description for the single authoritative statement of both parameters'
  effects.

## 3. `ReferenceTrade` schema

`ReferenceTrade` is a frozen value type (a plain `dataclass(frozen=True)`,
not a Pydantic model — it is a comparison fixture the later matching module
consumes, not a wire-serialized object, matching the style already used by
`rule_compiler.ExitIntent`/`PositionState`). Construction validates its own
invariants immediately and raises `ValueError` on violation (the same
fail-fast shape as `ExitIntent.__post_init__`), rather than admitting an
inconsistent record silently.

| Field | Type | Corresponding `TradeRecord` field | Notes |
|---|---|---|---|
| `trade_num` | `int` | `trade_num` | 1-based, assigned in emission order. |
| `symbol` | `str` | `symbol` | Verbatim. |
| `side` | `Literal["long", "short"]` | `side` | Verbatim (production stores a plain `str`; this is the stricter reference form). |
| `entry_bar` | `int` | *(none — new)* | Index into `bars[symbol]` where the position opened — one bar after the entry rule's trigger bar (see §5's "Entries" subsection). Unique only **within a symbol**, not globally — together with `symbol`, the key for this module's per-position bookkeeping (ladder-rung state, running aggregates); `trade_num` (above) remains the record's globally unique identifier. |
| `entry_rule_index` | `int` | *(none — derived from `entry_reason` today)* | Which `spec.entry_rules[i]` matched — mirrors production's `entry_reason = f"engine_entry:entry[{rule_idx}]"`, which (unlike `exit_reason`) passes through to `TradeRecord.entry_reason` unmodified, with no reconciliation step. Lets the later matching module detect a same-looking trade opened by a *different* entry rule when a spec has multiple same-side entry predicates. |
| `exit_bar` | `int` | *(none — new)* | Index into `bars[symbol]` where the position's **final** closing event occurred. For a position that passed through one or more `scaled_take_profit` rungs before closing, this is the bar of the final close only — never an earlier rung's bar — since the reference model emits one aggregated row per fully closed position (§5's "Exit aggregation" subsection). |
| `entry_date` | `str` | `entry_date` | `bars[symbol][entry_bar].timestamp[:10]` — truncated to the date portion exactly as production does (`pos.entry_timestamp[:10]`), so an intraday `Bar.timestamp` still matches production's date-only comparison key. |
| `exit_date` | `str` | `exit_date` | `bars[symbol][exit_bar].timestamp[:10]`, truncated the same way (`bar.timestamp[:10]` in `_fill_exit`). |
| `entry_price` | `float` | `entry_bid_price` | Pre-slippage reference level — **not** `entry_fill_price` or the legacy `entry_price` alias (both are post-slippage in production). See rationale below. |
| `exit_price` | `float` | `exit_bid_price` | Pre-slippage reference level. For a position closed in one shot this is that close's reference price; for a position that passed through one or more `scaled_take_profit` rungs first, this is the quantity-weighted average across every partial exit and the final close (§5's "Exit aggregation" subsection), mirroring `pos.weighted_avg_exit_bid_price` — the **pre-slippage** weighted average, **not** `pos.weighted_avg_exit_price`, which weights the post-slippage fill prices and is what production stores in `exit_fill_price`/the legacy `exit_price` alias. |
| `qty` | `float` | `shares` | Equals the position's entry quantity (`original_qty`), **not** the remaining size after any partial rungs — mirrors production's `TradeRecord.shares = pos.original_qty`. Entry quantity resolution is specified in §5's "Entries" subsection. |
| `exit_rule_kind` | `Literal["stop_loss", "take_profit", "scaled_take_profit", "signal_exit", "bracket_stop_loss", "bracket_take_profit"]` | *(none — derived from `exit_reason` today)* | The exact §4 vocabulary as a `Literal` — same stricter-typing rationale as `side` above, and validated against this same set in `__post_init__` alongside the other invariants. Always populated — every closed position in this model closes via some exit rule (there is no strategy-emitted arbitrary close path here). Describes only the position's final closing event (§5's "Exit aggregation"), not any earlier partial rung. |
| `exit_rule_index` | `int` | *(none)* | Which `spec.exit_rules[i]` fired the final close — mirrors `ExitIntent.rule_index`. |
| `level_index` | `Optional[int]` | *(none)* | Set only when the position's final closing event was itself a `scaled_take_profit` rung, identifying which rung — mirrors `ExitIntent.level_index`. `None` whenever some other rule kind performed the final close, even if earlier rungs fired first. |

**Why `entry_price`/`exit_price` map to the bid fields, not the fill fields:**
production's `entry_fill_price`/`exit_fill_price` (and their legacy
`entry_price`/`exit_price` aliases) already have slippage baked in — they
are approximately `entry_bid_price × (1 ± total_slip_bps / 10_000)`, per
[`trade_record_schema.md`](./trade_record_schema.md), but note that is a
*characterization*, not the derivation: production computes the bid and the
fill as two **independent** roundings of the same unrounded reference
price (`entry_bid_price = round(ref_price, dp)` alongside `fill_price =
round(ref_price * slip, dp)`), never by scaling the already-rounded bid.
Anything this module derives from a post-slippage basis must follow that
same shape — round once, at the end, from the unrounded reference price —
since rounding first and scaling second can differ in the last decimal
place (e.g. `ref_price = 9.99995` at 2 bps: production's
`round(9.99995 × 1.0002, 4) = 10.0019`, versus `round(9.99995, 4) × 1.0002
= 10.0020`), which is enough to shift a `basis="entry_price"` level and
potentially which bar crosses it. Since this module
explicitly excludes slippage/cost modeling (§1), its own `entry_price`/
`exit_price` are the pre-slippage reference levels — directly comparable to
production's `entry_bid_price`/`exit_bid_price`, not the fill prices.

**Rounding.** Production rounds `entry_bid_price`/`exit_bid_price` — 4
decimal places when the price is below $10, 2 decimal places otherwise
(`dp = 4 if ref_price < 10 else 2`) — before storing them.
`ReferenceTrade.entry_price`/`exit_price` must be rounded the same way, or a raw
percentage-derived level (which commonly carries more decimal places than
either rounding bucket allows) will show as a spurious mismatch against
production's own rounded fields for every single trade. For an aggregated
`exit_price` (§5's "Exit aggregation" subsection — a position closed via
one or more `scaled_take_profit` rungs before its final close), the
rounding bucket is chosen from the **final closing slice's own** reference
price, not from the weighted average itself — production computes `dp`
from the terminal slice's `ref_price`, then rounds the weighted-average
value using that `dp`, so a ladder whose earlier rungs filled below $10 and
whose final close fills above it still rounds to 2 decimal places overall
(and vice versa), never re-deriving `dp` from the blended value.

**Why both a bar index and a date string:** `entry_bar`/`exit_bar` are this
module's own primary keys — needed internally to track ladder-rung
sequencing and per-position running state (trailing watermarks, stop-limit
arm/latch) across the `bars` sequence. `entry_date`/`exit_date` exist because
they are what a `TradeRecord` actually has; the later matching module keys
its trade-to-trade comparison off `(symbol, entry_date, exit_date, side)`-
style fields, not bar indices, since production carries no bar index at all.
Carrying both from the start means the matching module never has to derive
one from the other.

**Deliberately excluded** (production `TradeRecord` fields this schema
does not carry, grouped by why):

- `position_value`, `gross_pnl`, `net_pnl`, `return_pct`, `outcome`,
  `cumulative_pnl`, `entry_fill_price`, `exit_fill_price`,
  `entry_order_type`, `exit_order_type`, `participation_clipped` —
  downstream cost/execution-mechanics fields, out of scope per §1.
- `partial_fill_count`, `total_unfilled_qty` — this module never models
  order-book/partial-fill mechanics at all (§1's "Order-book / partial-fill
  mechanics" exclusion), so there is nothing to populate these from.
- `hold_days` — trivially derivable by the matching module from
  `entry_date`/`exit_date`, unlike the two fields above.
- `entry_reason`/`exit_reason` free text — superseded by the structured
  fields: `entry_reason` by `entry_rule_index` (§3's field table above),
  `exit_reason` by the `exit_rule_kind`/`exit_rule_index`/`level_index`
  triple (also above).

### Invariants (as a value object)

Every invariant in this section is enforced in `__post_init__` (raising
`ValueError` on violation), not merely guaranteed by `simulate`'s own
construction path — a `ReferenceTrade` cannot exist in an invalid state
regardless of caller (direct construction in tests, the matching module's
translation adapters, or anywhere else), the same fail-fast shape
`ExitIntent` already uses. The two invariants called out below as
`Literal`-typed additionally get runtime membership checks precisely
because a `Literal` annotation alone is not enforced on a plain dataclass;
every invariant other than those two has no type-level backstop at all, so
its `__post_init__` check is the *only* enforcement, not a redundant one.

- `entry_bar < exit_bar` (strict — see §2's Postconditions for why no
  modeled exit can complete on `entry_bar` itself).
- `qty > 0`.
- `entry_price > 0` and `exit_price > 0`.
- `side in ("long", "short")`.
- `entry_rule_index` is always populated (every emitted `ReferenceTrade`
  represents a position that actually opened via some matched entry rule).
- `exit_rule_kind` and `exit_rule_index` are always populated (every emitted
  `ReferenceTrade` represents a fully closed position, and full closure
  always happens via some exit rule firing), and `exit_rule_kind` is always
  one of the six §4 vocabulary values — validated in `__post_init__`
  against that same set, the same stricter-typing treatment `side` gets
  above (a `Literal` annotation alone is not runtime-enforced on a plain
  dataclass).
- `level_index is not None` **if and only if**
  `exit_rule_kind == "scaled_take_profit"` — like every other invariant in
  this section, this is a `__post_init__` check on the constructed
  `ReferenceTrade` itself (raising `ValueError` if a value with
  `exit_rule_kind == "scaled_take_profit"` and `level_index is None` reaches
  it, or vice versa), not merely a property §5's fill semantics are trusted
  to uphold unchecked. §5 independently gets this right by construction: the
  forward direction (every `scaled_take_profit` close identifies its fired
  level) holds because §5's `scaled_take_profit` fill semantics set
  `level_index` to the fired rung on every such close, and the converse (no
  other exit kind ever populates `level_index`) holds because every other
  §5 fill semantics subsection leaves `level_index` unset. The
  `__post_init__` check is what turns a violation of either direction —
  whether from `simulate()` or from any other caller — into an immediate
  `ValueError` instead of a silently malformed record.

## 4. `exit_rule_kind` vocabulary

The base vocabulary is the existing `rule_compiler.ExitRuleKind` literal,
reused as the single source of truth rather than redefined:
`"stop_loss" | "take_profit" | "scaled_take_profit" | "signal_exit"`. Because
a `StrategySpec` can legally carry an `OcoBracketRule` as its sole price exit
(optionally alongside a `signal_exit`), this module extends that vocabulary
with two bracket-specific values so a bracket's legs are distinguishable in
the reference output the same way production distinguishes them:
`"bracket_stop_loss"` and `"bracket_take_profit"`.

| `exit_rule_kind` | Corresponding production `exit_reason` |
|---|---|
| `stop_loss` | `engine_exit:stop_loss` |
| `take_profit` | `engine_exit:take_profit` |
| `scaled_take_profit` | `engine_exit:scaled_take_profit` |
| `signal_exit` | `engine_exit:signal_exit[{exit_rule_index}]` |
| `bracket_stop_loss` | `engine_exit:bracket_sl` |
| `bracket_take_profit` | `engine_exit:bracket_tp` |

This module keeps `exit_rule_kind` prefix-agnostic — it does not construct or
depend on the `engine_exit:` string form, which is owned by the production
dispatcher this module must not import. The later trade-matching module,
which already needs `trading_service/service.py` to interpret production
trades, owns translating between this table's two columns in both
directions.

## 5. Per-exit-rule-kind fill semantics

Each subsection specifies: the trigger condition (reusing the existing
pure evaluator's trigger logic), the fill price, and the fill bar.

### Entries

An entry rule's trigger bar is the bar at which `evaluate_entry_rules`
matches. Like `signal_exit`, an entry is a bar-close predicate decision, not
a resting order: it fills at the **next** bar's open —
`entry_bar = trigger_bar + 1`, `ReferenceTrade.entry_price =
bars[symbol][entry_bar].open`. If the predicate matches on the final bar of
`bars[symbol]`, there is no next bar to fill on and this module opens no
position for that trigger (the same end-of-data handling as `signal_exit`).
`evaluate_entry_rules` already returns `(rule, rule_idx)` — `rule_idx` is
`ReferenceTrade.entry_rule_index` (§3) directly, no separate derivation
needed.

**Preconditions:**
- `evaluate_entry_rules` has matched a rule for this symbol at the trigger
  bar (the bar-close predicate decision described above).
- The symbol is a member of `spec.target_symbols`, whenever that set is
  non-empty (Target-symbol gating, below).
- The symbol has no already-open position and no already-queued,
  not-yet-filled entry from a prior trigger (Suppression, below).
- `entry_bar = trigger_bar + 1` exists — the trigger bar is not the final
  bar of `bars[symbol]`.

**Postconditions:**
- If every gate below (nonpositive trigger close, nonpositive fill-bar
  open, the position-cap clamp driving quantity to zero, an admission
  gate, fill-time capital sufficiency) passes: exactly one position opens
  at `entry_bar`, priced at `bars[symbol][entry_bar].open`
  (`ReferenceTrade.entry_price`), sized per the sizing/clamp/whole-share
  rules below, with `entry_rule_index` equal to `evaluate_entry_rules`'s
  `rule_idx`.
- If any gate below fails: no position opens and no `ReferenceTrade` is
  emitted for that trigger — the trigger is simply dropped, not retried on
  a later bar.

The remaining paragraphs in this subsection detail how each of those gates
and the fill price/quantity are computed; they elaborate the postconditions
above rather than adding new contract surface.

**Suppression while a position is open or pending.** An entry rule that
keeps matching on later bars while the symbol already has an open position,
or already has an entry queued from a prior bar's trigger not yet filled,
must **not** open a second, overlapping position — mirrors
`_EngineEntryDispatcher.maybe_emit`'s two gates (`portfolio.positions.get(sym)
is not None` and an already-queued same-symbol entry). Without this, a
predicate that stays true for several consecutive bars would open one
production position but many overlapping reference ones.

**Target-symbol gating.** When `spec.target_symbols` is non-empty, an entry
rule is evaluated **only** for symbols in that set — mirrors `maybe_emit`'s
early return (`if self.target_symbols and cur_bar.symbol not in
self.target_symbols: return`), which skips predicate evaluation entirely,
before any trigger check, for a symbol outside it. A `bars` mapping that
happens to include auxiliary symbols beyond `spec.target_symbols` (e.g. a
benchmark or a signal-only symbol) must not have entries opened against
those extra symbols by this rule.

**Nonpositive (or non-finite) close.** `Bar` does not itself validate OHLC
values as positive or finite, so a trigger bar with `close <= 0` — or
`NaN`/`+inf`, which no `<= 0` comparison catches (`NaN <= 0` is `False` in
Python, same as `NaN > 0`; `+inf <= 0` is also `False`) — is not excluded by
this document's own preconditions. (`-inf` is the one non-finite case the
existing `<= 0` guard already catches on its own — `float('-inf') <= 0` is
`True` in Python — so it needs no separate handling.) Mirror
`_compute_qty`'s own guard for the ordinary nonpositive case
(`trigger_bar.close <= 0` sizes the entry to zero and no position opens),
and additionally require `math.isfinite(trigger_bar.close)`
before using it in any sizing division: a `NaN`/`inf` close must produce
the same no-position outcome, never a `NaN`/`inf` quantity silently
propagated downstream. This one non-finite case does widen beyond
`_compute_qty`'s literal `close <= 0` line — it is a robustness guard
against garbage input this document's own preconditions don't exclude, not
a claim that production has an equivalent explicit finiteness check.

**Nonpositive (or non-finite) fill-bar open.** This guard is distinct from,
and does not subsume, the one above: `_compute_qty`'s `close <= 0` check
only covers the *trigger* bar's close used for sizing, not the separate
*fill* bar (`entry_bar = trigger_bar + 1`) whose `open` becomes
`ReferenceTrade.entry_price`. Production has no corresponding guard on
that fill-bar open either. Two distinct non-finite cases motivate the
check, and they fail the `> 0` test differently: a `NaN` open violates the
`ReferenceTrade.entry_price > 0` value-object invariant (§3) outright
(`NaN > 0` is `False`, the same non-catching problem as above), while a
`+inf` open *satisfies* `> 0` (`float("inf") > 0` is `True`) yet is not a
usable price — it would poison every downstream figure derived from it
(mark-to-market equity, the capital ledger, trade matching). A bare `> 0`
check is therefore not sufficient; the `math.isfinite` half is what catches
`+inf`. This module must check
`not (bars[symbol][entry_bar].open > 0 and
math.isfinite(bars[symbol][entry_bar].open))` itself and,
if true, skip opening a position for that trigger — the same no-position
outcome as a nonpositive trigger-bar close, just guarding the other price
this module reads before a `ReferenceTrade` can be constructed. Because
production lacks this guard, a degenerate fill-bar open (`<= 0` **or**
non-finite — production has no `math.isfinite` check either) yields a
**production** trade with no reference counterpart at all — the later
trade-matching module must treat any production trade whose entry price is
not a positive finite number as an expected unmatched production trade,
not a matching failure. The same widened guard on the *trigger*-bar close
(above) raises a related but **different** question: `_compute_qty`'s own
`close <= 0` check does not catch a `NaN` trigger close either (`NaN <= 0`
is `False`), so `equity * fraction / NaN` (or the equivalent for the other
sizing kinds) propagates `NaN` into `qty` —
`_EngineEntryDispatcher.maybe_emit`'s own `qty <= 0` early-return (the
`risk_capped_skip` path) does not catch it either, for the same reason.
What happens next **depends on the asset class**, and the two branches
diverge sharply:

- **Fractional asset classes** (crypto, forex) return `qty if qty > 0.0
  else 0.0`. `NaN > 0.0` is `False`, so the `NaN` collapses to `0.0` and
  the entry is skipped — no production trade.
- **Whole-lot asset classes** (equities, futures, commodities) route
  through `_floor_or_skip_whole_share`, which **turns the `NaN` into
  exactly one share**. `NaN >= 1.0` is `False`, so it falls to the
  one-share probe: `_cap_qty_to_position(1.0, equity=..., close=NaN)`
  computes `max_qty = equity * pct / 100 / NaN = NaN` and returns
  `min(1.0, NaN)`, which in Python is `1.0` (the comparison `NaN < 1.0` is
  `False`, so `min` keeps its first argument). The probe therefore reports
  `one_share = 1.0 >= 1.0`, and the method returns `1.0`. That quantity is
  an ordinary positive float, so `OrderRequest`'s `qty: float = Field(gt=0)`
  accepts it, the entry fills, and production emits a perfectly
  normal-looking one-share `TradeRecord`.

So on a whole-lot asset class a `NaN` trigger-bar close **does** silently
complete a production trade, exactly the way a degenerate fill-bar open
does above. This module's `math.isfinite` guard emits nothing for it, so
the matching module must treat it the same way: **an expected unmatched
production trade, not a matching failure.** Concretely, the matching
module's "expected unmatched production trade" rule must cover a
production trade whose *trigger*-bar close is not a positive finite
number — not only one whose entry price isn't — since the emitted trade's
own entry price is perfectly ordinary in this case and gives the matcher
no signal on its own. Only the fractional branch skips.

**Quantity.** `simulate` resolves each entry's quantity from `spec.sizing`
against a running equity figure it tracks itself — seeded at
`starting_equity`, then marked to market at each entry-sizing decision using
the **latest observed price of every currently open position across all
symbols** (unrealized value included, not just realized/closed reference
trades) — mirroring `Portfolio.mark_to_market()`, which values open
positions the same way. This is **not** a no-slippage or no-cost
abstraction — see §1's "Cost-aware position sizing" bullet: this equity
figure is capital (itself post-slippage) plus this unrealized mark-to-market
term, matching production's own `equity = portfolio.mark_to_market() =
capital + mtm` exactly to the extent §1's other, narrower exclusions permit.
Identifying *which* price is latest does not by itself define a position's
equity contribution — the side-specific formula does, and it is **not**
symmetric: `equity += remaining_qty * price_now` for a long, but `equity +=
remaining_qty * (2 * entry_price_basis - price_now)` for a short (mirroring
`Portfolio.mark_to_market`'s own `pos.qty * (2 * pos.entry_price -
price_now)` branch — `pos.qty` there is the position's **currently open**
quantity, decremented by `Position.reduce` on every partial exit, not
`original_qty`; this module's mirror is the same internal `remaining_qty`
introduced in "Additional admission gates" below, **not**
`ReferenceTrade.qty`, which is deliberately the original entry quantity and
would overstate a partially-reduced position's contribution to equity the
same way it would overstate leverage exposure) — synthesizing an
unrealized-loss mirror as the short's own price rises, not
the raw `qty * price_now` the long side uses. "Latest observed price" is
precise, not approximate: for each open position, it is
the **close of the most recently processed bar for that symbol** as of the
current point in the merged walk — mirrors `Portfolio.update_last_price`,
which stamps `last_price[symbol] = bar.close` every time a bar for that
symbol is processed, so `mark_to_market()` reads whatever close was last
recorded for a symbol even when the walk has since moved on to other
symbols' more recent bars. A symbol with no processed bar yet values at its
own `entry_price` (mirrors `last_price.get(sym, pos.entry_price)`'s
fallback) — though that case cannot arise for a symbol contributing to
equity, since a position must already be open (i.e., already past its own
entry) to be marked at all. This is why the "Cross-symbol processing order"
note above matters: computing this equity figure correctly for a
multi-symbol spec requires walking every symbol's bars in one merged
chronological order, not resolving one symbol's trades in isolation. The
per-sizing-kind formula mirrors production's, evaluated against the trigger
bar's close (the same anchor price production uses for sizing; the
reference fill itself resolves one bar later, at `entry_bar.open`, per the
"Entries" subsection above — sizing and fill price are not the same price):

- `FixedFractionSizing`: `equity * fraction / trigger_bar.close`.
- `FixedNotionalSizing`: `notional_usd / trigger_bar.close`.
- `VolatilityTargetSizing`: `equity * target_annual_vol / (trigger_bar.close * atr)`,
  where `atr` comes from the same indicator view this module
  already needs for `signal_exit` predicates (see §2's `PandasHistoryView`
  dependency note) — reinforcing that dependency rather than introducing a
  new one. **This formula is dimensionally odd and must be used exactly as
  written, not "fixed."** `atr` (below) is dollar-denominated (a mean true
  range, not a normalized fraction), so `equity * target_annual_vol /
  (close * atr)` does not reduce to a clean shares-unit analysis — but it is
  production's own `_compute_qty` expression, verified character-for-character
  (`raw_qty = equity * float(sizing.target_annual_vol) / (close *
  atr_val)`). An implementer who "corrects" this to divide by a normalized
  `atr / trigger_bar.close` instead would produce a reference ledger that
  systematically diverges from production's real sizing by a factor of
  `trigger_bar.close` — reproducing production's actual computation is this
  module's entire purpose, dimensional oddity included.

  **Which ATR is fully determined, not left to the implementer's choice.**
  "First" means: scan the entry rules' own predicates first (in
  `spec.entry_rules` list order), then the signal-exit rules' predicates (in
  `spec.exit_rules` list order); within one rule's predicate tree, take the
  first `IndicatorRef` named `"atr"` in leaf order (each leaf predicate's
  left side before its right side, leaves visited in the tree's own
  traversal order) — mirrors `iter_tree_indicator_refs`'s yield order
  exactly, so a predicate with several differently-configured ATR refs still
  resolves to one determinate choice. Fall back to `IndicatorRef(name="atr",
  params={"period": 14})` when no rule references an ATR at all — this
  indicator has no smoothing-method or source-field parameters to specify
  (`allow_source=False` in its registry entry): it is the unweighted mean of
  true range (`max(high-low, |high-prev_close|, |low-prev_close|)`) over the
  trailing 14 bars, not a Wilder-smoothed average. Warmup fallback: if the
  resolved ATR is unavailable or non-positive at the trigger bar (not enough
  history yet), fall back to a one-share probe instead of failing, then
  still run it through the whole-share/`max_position_pct` handling below.

**Percentage fields are not uniformly scaled.** `fraction` and
`target_annual_vol` above are decimal fractions (`0.10` = 10%, matching
`FixedFractionSizing.fraction`/`VolatilityTargetSizing.target_annual_vol`'s
own field bounds); `max_position_pct` in the clamp below is a whole-number
percentage (`6.0` = 6%, matching its own `le=100` field bound and the `/
100` in `_cap_qty_to_position`). `max_symbol_concentration_pct` (used
below, in "Additional admission gates") is the same whole-number
convention as `max_position_pct` — `Field(default=20.0, ge=0, le=100)` on
`RiskLimits`, and `RiskFilter.can_enter` itself computes `concentration =
notional / current_equity * 100` before comparing it against the raw field
value, so the field is compared directly against a percentage, not a
fraction. `max_gross_leverage` (used below, in "Additional admission
gates") is different again — a **decimal multiplier**, not a percentage at
all: `Field(default=1.0, ge=0)` on `RiskLimits` (no `le=100`, unlike the
two `_pct` fields above), and `RiskFilter.can_enter` compares
`total_notional / current_equity` directly against the raw field value
with no `* 100`/`/ 100` anywhere — so `1.0` means "cap gross notional at
1x equity," not "1%." Treating a field from one convention as if it belonged
to another — e.g. reading `fraction`/`target_annual_vol`'s decimal-fraction
value as a whole-number percentage, or `max_gross_leverage`'s decimal
multiplier as a `_pct` field's percentage — silently over- or under-sizes
every position (or mis-evaluates every gate) by a factor of 100. Confusing
two fields that share the *same* convention instead (e.g. `max_position_pct`
for `max_symbol_concentration_pct`, both whole-number percentages) is a
different bug — the wrong threshold applied, not a magnitude error — and is
not covered by this factor-of-100 warning.

**Position-cap clamp, applied first.** Before any whole-share handling, the
raw quantity from every sizing kind above — not just a sub-1 result — is
clamped so its notional does not exceed `equity * max_position_pct / 100`
at the trigger bar's close: `qty = min(raw_qty, equity * max_position_pct /
100 / trigger_bar.close)`, mirroring `_cap_qty_to_position`. This applies
unconditionally to all three sizing kinds this module models (fixed-fraction,
fixed-notional, volatility-target) — a `FixedNotionalSizing`
or `VolatilityTargetSizing` result above the cap must be reduced, not passed
through uncapped.

**Additional admission gates beyond the position-size cap — checked at the
fill bar, not the sizing/trigger bar.** `max_position_pct` is not the only
limit a real entry must clear. `spec.risk_limits` (the same model this
module already reads `max_position_pct` from) also carries
`max_open_positions`, `max_gross_leverage`, and `max_symbol_concentration_pct`
— production applies all of these via `RiskFilter.can_enter`, called from
`_fill_entry` at the **fill bar** (`entry_bar`), not at sizing time
(`trigger_bar`) alongside the position-cap clamp above. This module must
apply the same gates at the same point — `entry_bar`, together with the
capital check below — using its own tracked state, **and in this order**:

1. **`max_open_positions` first.** `RiskFilter.can_enter`'s very first
   check is `len(open_positions) >= max_open_positions`, ahead of even the
   nonpositive-equity rejection below. Reject the entry (open no position
   for that trigger) when the count of already-open positions has reached
   the cap.
2. **Unconditional nonpositive-equity rejection, checked before any ratio.**
   `RiskFilter.can_enter` rejects any entry outright when `equity <= 0`,
   ahead of and regardless of the ratio checks below — a percent-of-equity
   cap admits no positive position against a ruined account, and the
   leverage/concentration ratios are undefined (or, worse, falsely
   satisfiable) with a nonpositive denominator. This module must apply the
   same check here: if the tracked equity figure (§5's "Quantity" paragraph
   above) is `<= 0` at `entry_bar`, reject the entry without evaluating
   `max_gross_leverage`/`max_symbol_concentration_pct` at all.

   The admit/reject *outcome* does not depend on the relative order of
   gates 1 and 2 (either way the entry is rejected), so this ordering
   matters only for reproducing production's **rejection reason** —
   relevant to anyone reconciling reference-side rejections against
   production's `risk_gate:<reason>` diagnostics, where a ruined account
   that has also hit the position cap reports
   `max_open_positions (N) reached`, not the nonpositive-equity reason.
3. Reject the entry if it would push gross notional exposure past
   `max_gross_leverage * equity`, or this single symbol's notional past
   `max_symbol_concentration_pct / 100 * equity` — the `/ 100` is required
   (see the "Percentage fields are not uniformly scaled" note above):
   `max_symbol_concentration_pct` is a whole-number percentage field, the
   same convention as `max_position_pct`, not a decimal fraction.

**Only the leverage check carries an existing-exposure term; the
concentration check does not.** `RiskFilter.can_enter` builds
`total_notional = sum(position_value for open positions) + notional` and
uses it for the `max_gross_leverage` ratio **only**; the concentration
ratio it compares against `max_symbol_concentration_pct` is
`notional / current_equity * 100` — the candidate's own notional alone,
with no term for already-open positions (not even for another position in
the same symbol). Applying the existing-exposure sum to both ratios would
reject entries production admits: three open positions at 15% of equity
each plus a 10% candidate yields 55% against a 20% cap, dropping a trade
from the reference ledger entirely. Confine the sum below to the leverage
ratio's numerator.

For that leverage numerator, the notional basis is **not** uniform across
the two sides, mirroring production's `Position.position_value = entry_price
× qty` (post-slippage) vs. the candidate's own pre-slippage fill notional —
**and, on the existing-exposure side, `qty` there is production's
currently-open (already-reduced) quantity, not the position's original
entry size**: `Position.qty` decreases in place on every partial exit
(`Position.reduce`), so `position_value` reflects only what remains open.
This module's `ReferenceTrade.qty` (§3) is deliberately the **original**
entry quantity (matching production's `TradeRecord.shares`), so it is the
**wrong** value for this sum — this module must separately track each open
reference position's own live `remaining_qty` (starting at the entry
quantity, decremented by each fired `scaled_take_profit` rung the same way
`Position.reduce` decrements `qty`) and use that, not `ReferenceTrade.qty`,
here. Concretely: sum each **existing** open reference position's
`remaining_qty * entry_price_basis` (the internal post-slippage anchor,
§5's `stop_loss` subsection) for the leverage numerator's existing-exposure
term, but for the candidate's own contribution — in **both** ratios — use
`qty * bars[symbol][entry_bar].open`, the **raw, unrounded** fill-bar open, not
the rounded `ReferenceTrade.entry_price` output field — mirroring
production, which computes this same check from `terms.reference_price`
(`bar.open` for a market entry, itself never rounded) before `entry_bid_price
= round(ref_price, dp)` (§3) is even derived. A boundary case (e.g. a raw
open of `10.004`, stored as `10.00`) can be admitted or rejected differently
depending on which of the two values is used, so this module must read the
same unrounded price production's own gate reads, not its own rounded output
field. This is the same asymmetry `RiskFilter.can_enter` itself has, not an
arbitrary simplification.

**Fill-time capital sufficiency.** At that same fill bar, production also
re-checks affordability: `FillSimulator._fill_entry` rejects an entry when
`portfolio.capital < filled_qty * reference_price` — the sized notional may
no longer be affordable by the time the entry actually fills (a gap up from
the trigger bar's close, or another symbol's entry consuming cash first in
the same merged walk). This requires tracking a **second** running figure
alongside equity: available **capital** (cash), separate from equity (cash
plus unrealized position value) — mirroring `Portfolio.capital` vs.
`Portfolio.mark_to_market()`. Like gates 1-2 above, this is a pure
admission predicate with no side effect other than reject/admit, so it may
be evaluated before, after, or interleaved with them — the admit/reject
outcome is identical regardless of order, since production itself runs
`RiskFilter.can_enter` and this affordability check as two independent
checks inside `_fill_entry`, both ahead of applying slippage.

The check and the decrement deliberately use **different** bases, mirroring
production exactly rather than an inconsistency to reconcile: the
admission check reads `capital < qty * bars[symbol][entry_bar].open` — the
**raw, unrounded** fill-bar open (production's `reference_price` is
`ref_price`, computed before `fill_price` even exists and never rounded,
since `_fill_entry` runs this check ahead of applying slippage or the
`entry_bid_price` rounding), **not** the rounded `ReferenceTrade.entry_price`
output field — but once the entry is admitted, capital actually decreases by
the **post-slippage** fill notional (`qty * entry_price_basis`, mirroring
`Portfolio.open`'s `capital -= position.position_value` where
`position_value` is `Position.entry_price * qty` and `Position.entry_price`
is itself post-slippage) — never by `qty * entry_price` either. Exits
increase capital by their own post-slippage proceeds, computed the same
way but with the exit-side sign and reference price: an internal
`exit_price_basis = round(raw_exit_reference_price * (1 - entry_slippage_bps
/ 10_000), dp)` for a long, `round(raw_exit_reference_price * (1 +
entry_slippage_bps / 10_000), dp)` for a short — sign **flipped** from the
entry-side formula (mirroring `_slippage_multipliers`: a long exit receives
*less* via slippage, a short exit pays *more*, the reverse of each side's
entry case). Despite its name, `entry_slippage_bps` is the **one** input
this module has for slippage magnitude and governs both directions,
mirroring production's own single `config.slippage_bps`, which
`_slippage_multipliers` derives all four (entry-long, exit-long,
entry-short, exit-short) multipliers from. `raw_exit_reference_price` is the
same pre-rounding reference price §5's per-exit-rule-kind subsections
already compute before deriving the rounded, pre-slippage
`ReferenceTrade.exit_price` output value for that closing event (the
worse-of-open-and-level price for `stop_loss`, the exact target for
`take_profit`/`scaled_take_profit`, the next-bar open for `signal_exit`) —
capital increases by `filled_qty * exit_price_basis` on each exit slice,
never by `filled_qty * exit_price`. If
`capital < qty * bars[symbol][entry_bar].open` at the entry's actual fill
bar, the entry does not open (no position, no `ReferenceTrade`), even though
the sizing-bar checks above
already passed; production's own affordability check is this
slightly-optimistic pre-slippage estimate, not a false-safety gap this
module should "fix" by checking the post-slippage figure instead.

**Fractionality source.** Whether an asset class trades in fractional units
is a single flag derived from `spec.asset_class` (not a per-symbol
setting) via the same predicate production uses: `is_fractional_asset_class`
checks the spec's normalized asset class against a fixed whole-lot set,
`WHOLE_LOT_ASSET_CLASSES = {"stocks", "futures", "commodities"}` — note that
`"options"` is **not** in this set, so this codebase classifies options as
fractional-capable, the same as crypto/forex, even though options trade in
whole contracts in reality; this module must match that classification
exactly rather than the intuitively-expected whole-lot grouping. This one
flag applies uniformly to every symbol `bars` covers, since
`StrategySpec.asset_class` is a single spec-wide field, not indexed by
symbol.

Whole-lot handling, applied after the clamp above, mirrors production's
cap-aware floor rather than a blanket skip: a clamped quantity `>= 1` floors
down to `int(qty)`. A clamped quantity `< 1` on a non-fractional
(`WHOLE_LOT_ASSET_CLASSES`) asset class is **promoted to one whole unit**
(one share for stocks, one contract for futures/commodities) if that one
unit still satisfies `max_position_pct` (re-checked at exactly one unit,
since flooring up can itself re-breach the cap), and only **skipped** (no
entry) if even one unit would breach it. A fractional-capable asset class
(per `is_fractional_asset_class` above — crypto, forex, and, per this
codebase's classification, options) instead keeps the clamped, unfloored
quantity as-is (dropped to zero only if the cap itself drove it to zero or
below).

### Exit aggregation

A position may be reduced by zero or more `scaled_take_profit` rungs before
it is finally, fully closed — either by the ladder's own last rung, or by
an unrelated full-position exit rule (`stop_loss`, `take_profit`,
`signal_exit`, or an `oco_bracket` leg) firing first and closing all
remaining quantity in one shot. Every other exit rule kind is always a
full-position close; only `scaled_take_profit` rungs are partial.

`simulate` emits a `ReferenceTrade` **only when a position is fully
closed** — mirroring `FillSimulator._fill_exit`, which returns
`trade_record=None` for every partial close and builds exactly one
`TradeRecord` only once `pos.is_closed`, using `pos.original_qty` and
`pos.weighted_avg_exit_price`. "Fully closed" is a **relative**, not exact,
tolerance test — production's `Position.is_closed` considers the position
closed once `cumulative_exit_qty + original_qty * FILL_QTY_REL_TOL >=
original_qty` (`FILL_QTY_REL_TOL = 1e-12`), not exact floating-point
equality to zero remaining. This module must apply the same relative test
for both a ladder rung's own closure check and the position's overall
final-closure check — a ladder whose `qty_fraction`s sum to something like
`0.999999999999997` instead of exactly `1.0` due to floating-point
accumulation must still be treated as fully closed and emit its
`ReferenceTrade`, not left open with no row (per this module's own
end-of-data handling for a genuinely still-open position). Concretely, for
that one emitted record:

- `qty` is the position's entry quantity (`original_qty`), unreduced by any
  earlier partial rungs.
- `exit_price` is the quantity-weighted average of every partial exit's
  price (each rung's fill, plus the final closing fill), weighted by the
  quantity each one closed — trivially just that single price when the
  position closes in one shot with no prior rungs.
- `exit_bar`/`exit_date` are the bar of the **final** closing event, not any
  earlier rung.
- `exit_rule_kind`/`exit_rule_index`/`level_index` describe **only** the
  final closing event — a `scaled_take_profit` ladder whose last two rungs
  fired sets `exit_rule_kind="scaled_take_profit"` with the last rung's
  `level_index`; a `stop_loss` that closes out the remainder after two
  earlier rungs already fired instead sets `exit_rule_kind="stop_loss"` with
  no `level_index`, even though rungs contributed to `exit_price`.

**Capital credit on terminal closure includes the tolerance-clamped
residual, not just the closing slice's own quantity.** Production credits
cash in two steps, not one: `partial_close` credits `exit_qty *
exit_price` (unrounded) for every slice as it fills, but the position's
*terminal* close additionally calls `Portfolio.close`, which credits
`round(pos.qty * final_exit_price, 2)` for whatever quantity is still
tracked as open at that moment — `pos.qty`, not the closing slice's own
requested/nominal size. Two details of that credit are load-bearing and
must be mirrored exactly:

- **The rounding is always `round(..., 2)`** — a flat two decimal places,
  independent of the `dp = 4 if ref_price < 10 else 2` bucket used for
  price fields (§3). `Portfolio.close` hardcodes `round(pos.qty *
  exit_price, 2)`; a sub-$10 symbol does **not** get a 4-decimal capital
  credit.
- **The price is the quantity-weighted average across every slice**, not
  the closing slice's own price: `_fill_exit` passes `final_exit_price =
  pos.weighted_avg_exit_price` into `Portfolio.close`. (This is the
  post-slippage weighted average — the capital ledger is a post-slippage
  figure throughout, unlike the pre-slippage `ReferenceTrade.exit_price`
  output field; see §3.) For a laddered close, using the final rung's own
  price instead would drift the ledger.

For an exact single-shot close the residual quantity is `0.0` and the
`pos.qty`-vs-slice distinction is invisible. It becomes visible for a
ladder whose `qty_fraction`s sum to slightly **less** than `1.0`, leaving a
nonzero `remaining_qty` after every rung has fired.

A caution on which tolerance governs that case, since two different
constants are in play and they are **not** interchangeable:
`LADDER_SUM_TOL` (`1e-9`) is a *validator* bound, and it only rejects
ladders that sum **above** `1.0 + LADDER_SUM_TOL` — it says nothing about
a shortfall, and an over-sum is in any case clipped to zero residual by the
`min(level.qty_fraction * original_qty, remaining_qty)` rule (§5's
`scaled_take_profit` subsection). The closure test is the separate
`FILL_QTY_REL_TOL` (`1e-12`) relative test above. So a shortfall ladder —
fractions summing to, say, `1.0 - 5e-10` — leaves `remaining_qty ≈ 5e-10 *
original_qty`, which is **far too large** for `FILL_QTY_REL_TOL` to absorb:
`Position.is_closed` is `False`, production leaves the position open, and
it emits **no** `TradeRecord` at all. This module must reach the same
conclusion — treating such a residual as "closed enough" would credit
capital for it and emit a `ReferenceTrade` with no production counterpart.
The terminal credit therefore applies only on a closing event that
genuinely satisfies the `FILL_QTY_REL_TOL` test; when it does, credit
capital for the internally-tracked `remaining_qty` at that point rather
than merely the sum of each rung's own nominal contribution.

A position still open at `bars[symbol]`'s last bar — including one holding
only a partially-reduced remainder from earlier rungs — produces **no**
`ReferenceTrade` at all, matching production's `open_position_entry_reasons`
handling rather than a synthetic force-close.

### Per-bar evaluation order

Three ordering rules this module must enforce, all to stay behaviorally
identical to production, not just directionally similar. The first two
both concern how a resting order and `signal_exit` interact on the same
bar (entry-bar eligibility, then same-bar precedence); the third is a
distinct concern — entry-vs-exit evaluation phase order:

- **Resting orders are not eligible on their own materialization bar;
  `signal_exit`'s trigger check is.** These are two different mechanisms
  with two different entry-bar behaviors, and this module must not collapse
  them into one rule:
  - A resting order — a bracket leg, and, per this document's target-state
    modeling, a standalone `stop_loss`/`take_profit`/`scaled_take_profit`
    rung once materialized — is stamped with its materialization bar's own
    timestamp and is skipped whenever its materialization bar isn't
    strictly earlier than the bar being evaluated (mirrors the bracket-child
    submission guard). Concretely: **not eligible on `entry_bar` itself,
    first eligible at `entry_bar + 1`** (and, for a `scaled_take_profit`
    rung materialized when an earlier rung fires, not eligible until the
    bar *after* that rung's own materialization bar).
  - `signal_exit` is unaffected by any of this — it stays the unchanged,
    dispatcher-evaluated predicate check production runs today, and that
    dispatcher's `just_opened` gate is `False` from the start for a market
    entry (`entry_order_type == "market"`, which is the only way this
    module ever fills an entry — see "Entries" above). So **`signal_exit`'s
    trigger check is eligible starting on `entry_bar` itself**; only its
    *fill* is deferred, by its own separate next-bar-open rule
    (`exit_bar = trigger_bar + 1`, already stated in `signal_exit`'s own
    subsection) — not because of any entry-bar restriction.
- **A reachable resting order beats a same-bar queued `signal_exit` close —
  FIFO by materialization time, not "queued fills always go first."**
  Production processes a symbol's pending orders in one FIFO walk ordered
  by submission time; once an earlier order in that walk closes the
  position, every later order in the same walk is dropped by the
  stale-position guard rather than also filling. A resting order was
  materialized at entry (or at its own rung's advance) — strictly earlier
  than any `signal_exit` trigger, which can only fire on some later bar —
  so on a bar where both are reachable, the resting order is walked first
  and wins: it fills, and the `signal_exit` close is discarded against the
  now-closed position rather than also filling. This is the reverse of
  "queued orders fill before this bar's own checks" as a blanket rule; it
  only holds when nothing has been resting on the book for this position
  already.
  - **Exception: a resting `style="limit"` stop-loss does not get this FIFO
    chance at all against a rule choosing a *whole-position* close — it is
    retired outright the moment such a close is chosen for the same
    position.** Production excludes a resting limit-style stop from its own
    exit evaluation once it is resting, so any intent chosen while it rests
    is necessarily a *different* rule; the moment that different rule's
    close is decided, production immediately cancels the resting
    stop-limit (mirrors `_retire_orders_against_closed_position`), before
    that other rule's close even reaches its own fill bar. So a
    `style="limit"` stop can never "win FIFO" against a later `signal_exit`
    close the way a bracket leg or a still-resting `market`-style
    stop/take-profit/scaled rung can — by the time any competing close
    would fill, this module must have already removed the `style="limit"`
    stop from consideration, the same bar the competing rule's intent was
    chosen. **This retirement is conditioned on the competing close being a
    whole-position close, not any competing intent whatsoever:**
    `_retire_orders_against_closed_position` is only reached from
    `_emit_full_close` (a full `stop_loss`/`take_profit`/`signal_exit`) or
    from `_emit_partial_scale_out` when the firing `scaled_take_profit` rung
    empties the position (`req.qty >= pos.qty` within `FILL_QTY_REL_TOL`) —
    a **genuinely partial** rung (one that leaves quantity open) does
    **not** retire the resting limit-style stop; it keeps resting,
    protecting the position's runner exactly as before. This module must
    apply the same qualification: retire a resting `style="limit"` stop
    only when the competing close is a full close or an emptying rung,
    never for a partial rung.
  - **Two standalone resting exits reachable on the same bar are broken by
    spec order, since FIFO cannot break that tie.** The FIFO rule above
    orders a resting exit against a *later*-materialized `signal_exit`
    close; it is silent when two standalone resting exits compete, because
    every standalone rule's order materializes at the same instant —
    `entry_bar` — so their materialization times are equal. (Example: a
    spec carrying both `take_profit(pct=0.05)` and `stop_loss(pct=0.03)`,
    on a wide bar whose `high` reaches the target *and* whose `low`
    reaches the stop.) This module must break that tie by **ascending
    `spec.exit_rules` index**, which is exactly what the Reuse-mandated
    `first_exit_intent_for_position` does (it walks rules in spec order and
    returns the first that fires), so the reference ledger and production
    pick the same rule. The bracket's "stop leg wins a same-bar
    double-touch" rule (§5's `oco_bracket` subsection) is the same
    principle applied within a single rule, where submission order rather
    than spec index sets the sequence — it is not a competing convention,
    and it does **not** generalize into "the stop always wins" for
    standalone rules. Without this tie-break the doc would admit two
    conforming implementations that emit different
    `exit_rule_kind`/`exit_rule_index`/`exit_price` for identical input,
    violating `simulate`'s own determinism Invariant (§2).
- **Exit evaluation for a symbol resolves before entry evaluation for that
  same symbol on the same bar, and entry suppression reads the
  post-exit state.** Production's own per-bar pipeline processes a bar's
  fills first, refreshes the position tracker from that post-fill
  portfolio, and only then evaluates `_EngineEntryDispatcher.maybe_emit` —
  so a symbol whose position closes via a fill on `cur_bar` is already flat
  by the time entry rules are evaluated for `cur_bar`, and a matching entry
  predicate on that same bar is **not** suppressed by the now-closed
  position. This module must walk each bar in the same phase order per
  symbol — resolve every exit-side outcome for that bar (including a
  resting order that fires and closes the position on its own trigger
  bar, per this document's target-state modeling) before evaluating that
  bar's entry rules — and the "Suppression while a position is open or
  pending" check in the "Entries" subsection above must read the resulting
  **post-exit** state, not a snapshot taken before this bar's exits were
  resolved. Getting this backwards would incorrectly drop a legitimate
  re-entry trigger on the very bar a position closes.

### Engine-injected short safety stop

Before evaluating any exit rule, this module must reproduce one
preprocessing step production applies to the spec: for any spec that permits
short exposure and has no already-effective short-side stop among its
existing exit rules, production **appends** a synthetic
`StopLossRule(pct=1.0, basis="entry_price")` to the working `exit_rules`
list, mirroring the same "no effective short stop" check production uses
(`first_side_stop_factor(exit_rules, "short") is None`). Production's own
condition for "permits short exposure" is the disjunction
`shorts_possible = entry_rules is None or any(rule.side == "short" for rule
in entry_rules)` (verbatim from `TradingService.__init__`) — the first
disjunct is the `requires_custom_code` signal, already excluded from this
module's scope per §2's precondition, so **only** the second disjunct
applies here: this module's check reduces to
`any(rule.side == "short" for rule in spec.entry_rules)`, i.e. any
`EntryRule.side == "short"`. This is a
**real, indexable** rule once injected — not a
side-channel default — so a short position that runs to double its entry
price closes via this synthetic rule in production, with a genuine
`rule_index` one past the last authored rule
(`len(spec.exit_rules)`), attributable exactly like any other `stop_loss`.
This module must perform the same injection into its own working rule list
before evaluating exits, or it will silently lack a real exit rule that
production's ledger actually fires.

### Nonpositive exit references

`Bar` does not validate OHLC values as positive or finite (§2's
Preconditions only require a strictly timestamp-increasing sequence), so a
bad bar can make any exit kind's computed fill price nonpositive or
non-finite — not just entries (§5's "Entries" subsection already guards
those two cases): a `signal_exit`'s fill-bar open can be `<= 0` (or `NaN`)
the same way an entry's can; a `stop_loss`'s gap-through fill (`bar.open`
on a gap) can be `<= 0` for a long; a resting limit's `entry_price_basis *
(1 ± pct)` target could in principle be driven non-positive by extreme
inputs even off a valid anchor; any of these can also be `NaN`/`±inf` if
the underlying bar field is, and `NaN <= 0` is `False` in Python — a plain
`<= 0` check does not catch it. `exit_price > 0` is a
`ReferenceTrade` value-object invariant (§3) that production has no
corresponding runtime guard for (it simply never encounters this in
practice) — this module cannot construct an invalid `ReferenceTrade` and
must not let one bad bar crash `simulate()` outright.

The uniform rule, applied by every per-exit-rule-kind subsection below: if
the fill price a rule would otherwise record for a closing event is not a
finite positive value (`<= 0`, `NaN`, or `±inf` — i.e.
`not (price > 0 and math.isfinite(price))`), that rule does **not** fire on
that bar — exactly as if its trigger condition had not been met. Evaluation
continues normally on subsequent
bars (other exit rules for the same position may still fire on the same or
a later bar; the same rule may fire later once it would compute a positive
price). This mirrors the "Nonpositive fill-bar open" entry guard above: a
degenerate bar suppresses one candidate fill rather than aborting the run.
A position that never receives a valid positive closing fill before
`bars[symbol]` ends produces no `ReferenceTrade`, identically to any other
position still open at the last bar (§2's Postconditions).

### `stop_loss`

Covers all `basis` × `style` combinations from `StopLossRule`. Unlike a
bracket leg (see `oco_bracket` below), a standalone stop/take-profit's
`basis="entry_price"` level is anchored to the position's actual fill —
these rules evaluate against the live position after it has actually
filled, not against a signal-time reference resolved before fill (that's
what distinguishes them from `oco_bracket`'s trigger-bar-close anchor
below).

**Anchor price is post-slippage, not `ReferenceTrade.entry_price`.**
Production's engine computes these levels against `Position.entry_price`,
which is the **post-slippage** fill — `round(raw_open × (1 ± slippage_bps /
10_000), dp)`, sign `+` for a long entry, `-` for a short, computed from the
**raw, unrounded** `bars[symbol][entry_bar].open` (the same order of
operations `trade_record_schema.md` documents for `entry_fill_price`, with
no order-type adverse-selection add-on since every entry here is a market
fill) — **never** derived by taking the already-rounded `entry_bid_price`
field and multiplying that by the slippage factor; see the
`entry_price_basis` definition below for why the two roundings must stay
independent. This module's own `ReferenceTrade.entry_price` output field is
the
**pre-slippage** bid (§3, by design, for direct comparability against
production's `entry_bid_price`) — so the two must not be conflated. This
module needs an internal-only `entry_price_basis = round(raw_open × (1 ±
entry_slippage_bps / 10_000), dp)` (using the `entry_slippage_bps`
parameter, §2), computed from the **raw, unrounded**
`bars[symbol][entry_bar].open` and its own `dp` (`4` if that raw value is
below `10` else `2`) — mirroring `fill_price = round(ref_price *
slip_multiplier, dp)`'s exact order of operations (multiply the raw
reference price by the slippage multiplier, *then* round once). Do
**not** compute it by taking the already-rounded `ReferenceTrade.entry_price`
output value and multiplying that by the slippage factor — production
derives `entry_bid_price` and the slipped fill as two independent roundings
of the same raw reference price, not one from the other, and a value near a
rounding-bucket boundary can genuinely differ between the two orders of
operation. Anchor every `basis="entry_price"` level, `take_profit`/
`scaled_take_profit` target, and the trailing-stop watermark seed below
against `entry_price_basis` — **never** against the emitted
`ReferenceTrade.entry_price` value directly. Anchoring against the
pre-slippage value instead would shift every such level (and potentially
which bar crosses it) away from where production's real engine actually
places its resting orders, for no reason other than this module's own
choice of comparison field — precisely the kind of self-inflicted,
trivial divergence this reference ledger exists to avoid.

- **`style="market"`** (any `basis`): fires the bar the price level is
  breached. Fill price is the level itself, or — on a gap where the bar's
  open already lies past the level — the bar's open (worse-of-open-and-level,
  the same rule the existing OCO-bracket stop-loss precedent follows). Fill
  bar is the trigger bar.
- **`basis="trailing_high"` / `"trailing_low"`**: the protective level ratchets
  favorably as price moves in the position's favor. This module must track
  its own per-position running watermark (`max` of `bar.high` since entry
  for a long's trailing-high stop, `min` of `bar.low` for a short's
  trailing-low stop) and re-derive the effective stop level from it each
  bar, since the reused decision evaluator is stateless per call and does
  not itself carry this history. **Seed value**: the watermark is
  initialized to `entry_price_basis` (the post-slippage anchor defined
  above) — **not** `ReferenceTrade.entry_price`, and **not** `entry_bar`'s
  own high/low — mirroring `_TrackedPosition`'s own initialization
  (`high_since_entry = low_since_entry = pos.entry_price`, production's
  post-slippage fill); including the entry bar's actual high/low here would
  be intrabar lookahead the same way including the current bar's would be
  (see below). Since this kind isn't eligible until `entry_bar + 1` (per
  "Per-bar evaluation order" above), the first trigger check — on
  `entry_bar + 1` — runs against this `entry_price_basis` seed, then extends
  with `entry_bar + 1`'s own high/low for the bar after that.
  **Evaluate-then-extend ordering matters** on every bar thereafter too:
  each bar's trigger check must run against the watermark **as of the prior
  bar** — not yet including this bar's own high/low — and only *after* that
  check does the watermark extend with this bar's high/low, for the next
  bar to see. Updating the watermark before the check would let a bar's own
  favorable extreme raise the stop and then have the same bar's opposite
  extreme trigger it, misreading an ordinary bar as a stop-out. Fill-price/
  fill-bar rule is otherwise identical to the static case above.
- **`style="limit"`**: modeled as a resting stop-limit (restricted, per
  `StopLossRule`'s own validation, to `basis="entry_price"` — a limit-style
  stop cannot trail). The limit sits on the protective side of the stop:
  `limit_price = stop_price - offset` when closing a long (a sell),
  `limit_price = stop_price + offset` when closing a short (a buy), where
  `offset = stop_price * limit_offset_pct` — the same sign convention as
  `protective_limit_price`. The stop triggers on the usual crossing test
  (bar reaches `stop_price` on the closing side); once triggered, it fills
  at the **exact limit price** the first bar the range reaches it (a sell
  triggers on `bar.low <= stop_price`, then fills once `bar.high >=
  limit_price`; a buy is the mirror image) — `ReferenceTrade.exit_price =
  limit_price`, never `stop_price`, and never gap-adjusted worse, same as
  `take_profit`'s exact-price rule. Reachability is judged on the triggering
  bar's full range, not its open: a bar that opens beyond the limit but
  whose range still reaches back to it **fills on that same bar** (a sell
  fills if `bar.high >= limit_price` anywhere in the bar; a buy if `bar.low
  <= limit_price`), regardless of where the open printed. Only a bar whose
  **entire range** stays beyond the limit leaves the order unfilled — it
  stays "armed," and only fills on a later bar whose range reaches the limit
  (the limit price itself is static once computed, since trailing bases are
  unavailable for `style="limit"`). This module models that arming/latching
  at the semantic level (a boolean per-position flag once the stop level is
  first breached), not by replicating the production `PendingOrder` state
  machine's exact fields.

### `take_profit`

Modeled as a resting limit at the exact target price
(`entry_price_basis * (1 + pct)` for a long, `entry_price_basis * (1 - pct)`
for a short — the post-slippage anchor `stop_loss` defines above, not
`ReferenceTrade.entry_price`). Always fills at
**exactly** the target — never better, never worse, even on a gap through
it — which is a deliberate
production design choice, not merely "a limit never fills worse than its
price" (that premise alone would permit *better*-than-target price
improvement on a gap, which this module must **not** model): the default
execution model's own limit-pricing rule is titled "Limit fills always at
the limit price when the bar's range covers it," and its docstring
explicitly rejects the alternative (`min`/`max`-of-open-and-limit) as "free
alpha" a live resting limit order would not actually receive — that
alternative exists only in a legacy, non-default execution model this
module does not target. `stop_loss`'s worse-of-open-and-level adjustment
and `take_profit`'s exact-price fill are therefore not an inconsistency —
both are the *default* execution model's real behavior, just asymmetric
because stops and limits behave asymmetrically by nature (a stop can gap
through to a worse fill; a limit either fills exactly or doesn't fill).
Fill bar is the trigger bar.

### `scaled_take_profit`

A ladder of resting limit orders, one per `TakeProfitLevel`, each at
`entry_price_basis * (1 + level.pct)` for a long, `entry_price_basis * (1 -
level.pct)` for a short (same post-slippage anchor as `take_profit`, not
`ReferenceTrade.entry_price`) — `level.pct` is a positive
magnitude, strictly increasing across `levels` by construction
(Pydantic-enforced), so
rung 0 is always the target closest to entry and the last rung the target
farthest away, on **either** side (a short's targets sit below entry, but
"closest" still means smallest `pct`, not most negative price). Each rung's
quantity is `min(level.qty_fraction * original_qty, remaining_qty)`, where
`remaining_qty` is the position's currently-open quantity at the moment the
rung fires — fixed at entry only in the `level.qty_fraction * original_qty`
sense, not a fraction of the live (already-reduced) position size, but still
clipped to what remains open. This clip is not a defensive nicety: a valid
ladder's `qty_fraction`s need only sum to `<= 1.0 + LADDER_SUM_TOL` (§2's
Contract), so a ladder whose sum lands fractionally above `1.0` (permitted by
that tolerance) would, without the clip, have its final rung request
slightly more quantity than the position has left — mirroring
`FillSimulator._fill_exit`'s own `fillable_qty = min(target_qty, pos.qty)`,
which exists for exactly this reason. Sequencing mirrors the
production ladder cursor's per-rule-index "next un-fired rung" counter: only
the **un-fired rung closest to entry** — i.e. the next rung in configured
ladder order, advancing outward — is eligible to trigger on a given bar, and
a single bar advances that rung's ladder cursor by exactly one rung even if
the bar's range would have cleared several rungs at once. This module must
maintain that same one-rung-per-ladder-per-bar advancement rule, where
"ladder" means one `(position, exit_rule_index)` cursor: a `StrategySpec`
may define more than one `scaled_take_profit` rule (each its own
`exit_rules` entry, hence its own `exit_rule_index`), and production's
cursor is keyed per `rule_index` so each ladder tracks its own position in
its own rung sequence. This module's cursor state must therefore also be
keyed per `(position, exit_rule_index)`.

Cursor *keying* is per-rule; the per-bar firing *budget* is per-position.
Even with two ladders attached to one position, **at most one rung fires
per position per bar** — production's exit dispatcher calls
`first_exit_intent_for_position` once per open position per bar and acts on
that single returned intent, so the second ladder's reachable rung is
simply not emitted that bar. Two further production behaviors reinforce the
same budget and must be mirrored: a rung is not offered at all while a
prior rung's scale-out is still in flight (production's
`scaled_partial_in_flight` deferral), and a single bar advances the firing
ladder's cursor by exactly one rung even when the bar's range clears
several. So this module must fire at most one rung per position per bar,
choosing it in the same ascending `spec.exit_rules` order the reused
evaluator walks (the same tie-break the "Two standalone resting exits
reachable on the same bar" bullet under "Per-bar evaluation order" above
specifies) — never every technically-reachable rung, and never one rung
per ladder.
Fill price for a firing rung follows the same exact-price rule as
standalone `take_profit`; fill bar is the trigger bar. A fired rung does
**not** emit its own `ReferenceTrade` — see the "Exit aggregation"
subsection above for how rungs feed into the single record eventually
emitted when the position is fully closed.

**Current-bar reachability is a separate, additional gate on top of
`_next_scaled_rung`'s eligibility check.** `_next_scaled_rung`'s reachability
test is history-based by design — the running watermark since entry, folded
with the current bar — so a rung a single spike bar clears past stays
*eligible* on every later bar even after price retraces, precisely so a rung
a gap cleared isn't lost. That answers only "has this rung's level ever been
reached," not "does *this* bar's own range reach it" — and the exact-price
fill rule above requires the latter, the same way `stop_loss`/`take_profit`
above only fire on a bar whose own range actually covers their level. So
before recording a fill for the cursor rung `_next_scaled_rung` reports as
eligible, this module must independently check the **current bar's own**
`high` (long) / `low` (short) against that rung's exact target price. If the
watermark says eligible purely from an earlier bar's history but the current
bar's own range does not reach the target, the rung does **not** fire this
bar — it remains the cursor rung, re-checked (both the watermark eligibility
and this current-bar reachability gate) on each subsequent bar until one
actually trades there. Without this gate, a bar trading entirely away from a
rung's level could still be recorded as an exact fill at that level purely
because an earlier bar's spike had already satisfied the reused evaluator's
watermark test — a fabricated fill on a bar that never traded there, which
this reference ledger must not produce.

### `signal_exit`

Unchanged from current (and post-resting-exit-epic) engine semantics: a
bar-close predicate decision, filled at the **next** bar's open. This is the
one exit kind where the fill bar differs from the trigger bar —
`exit_bar = trigger_bar + 1` — unlike every resting-order kind above, where
trigger bar and fill bar coincide. If the predicate fires on the final bar
of `bars`, there is no next bar to fill on; this module treats that as "no
trade emitted for this trigger" rather than fabricating a fill past the end
of the data. Unlike every resting-order kind above, `signal_exit`'s trigger
check is eligible starting on `entry_bar` itself, not `entry_bar + 1` — see
"Per-bar evaluation order" above for why entry-bar eligibility differs
between resting orders and this kind, and for the FIFO rule that lets a
same-bar resting order beat a queued `signal_exit` close.

### `oco_bracket`

Both legs are modeled as resting orders using the same rules as their
standalone counterparts: the stop leg (`BracketStopLeg`) follows the
`stop_loss` worse-of-open-and-level rule (its `style="limit"` variant follows
the same arm/latch behavior); the take-profit leg (`BracketTakeProfitLeg`)
follows the `take_profit` exact-price rule. The two legs are mutually
exclusive by construction — whichever fires first closes the whole position,
and the sibling leg is not evaluated further for that position. This module
must suppress the non-firing leg entirely (no `ReferenceTrade` emitted for
it), the same one-cancels-other behavior production implements by canceling
the sibling order once either leg fills. A `signal_exit` rule may legally
coexist alongside a bracket in the same spec and is evaluated independently
per §5's `signal_exit` rules above.

**Reference price — anchored to the trigger bar's close, not the entry
fill.** Unlike a standalone `stop_loss`/`take_profit`, both bracket legs'
percentage offsets resolve against the entry rule's **trigger bar's close**
(`bars[symbol][trigger_bar].close`, where `trigger_bar = entry_bar - 1` per
the "Entries" subsection) — the same reference price this module's own
entry-quantity sizing uses, and the same one production's bracket
attachment resolves against before the entry order has even filled. This
matters on a gap: if `entry_bar`'s open jumps away from `trigger_bar`'s
close, the bracket's stop/target levels do **not** shift to re-center on the
(possibly very different) actual fill price the way a standalone
`basis="entry_price"` stop would.

**Same-bar precedence.** A single bar's OHLC range can touch both legs'
levels at once, which the bar's own high/low/close alone cannot resolve —
"whichever fires first" is not observable from OHLC data. This module
breaks that tie the same way production's bracket materialization does (the
stop-loss child is submitted before the take-profit child, and pending
orders are then processed in that same order): **on a same-bar double-touch,
the stop leg wins.**

**Eligibility starts at `entry_bar + 1`, not `entry_bar` itself** — see the
"Per-bar evaluation order" subsection above. A bracket's levels can look
already touched by the entry bar's own range, but production's children are
stamped with the entry-fill bar's timestamp and cannot fire on that same
bar; this module must reproduce that deferral rather than closing the
position on `entry_bar`.

## 6. Forward references

This schema is designed to be consumed by a later trade-matching module that
diffs a `ReferenceTrade` list against a production trade list
(`TradeRecord`), using `entry_date`/`exit_date`/`symbol`/`side` as the
comparison key and `exit_rule_kind`/`exit_rule_index`/`level_index` to
confirm the two ledgers agree on *why* each trade closed, not just its price.
That module owns the `engine_exit:` string construction/parsing (§4) and any
tolerance banding for price comparison; neither concern belongs in this
schema or in the `simulate()` function this doc specifies.

**What production's `exit_reason` can and cannot confirm.** Production's
persisted `TradeRecord.exit_reason` is only `f"engine_exit:{kind}"` for
`stop_loss`/`take_profit`/`scaled_take_profit` — no `exit_rule_index` or
`level_index` suffix, unlike `signal_exit`'s
`f"engine_exit:signal_exit[{idx}]"`, and `TradeRecord` has no `level_index`
field at all. So the
matching module can only verify `exit_rule_kind` itself against production
for those three kinds — confirming *which specific* `spec.exit_rules[i]`
fired, or which ladder rung, is only possible when the production kind is
`signal_exit`. A spec with two `stop_loss` rules (an unusual but legal
shape) or a multi-rung ladder is therefore only partially checkable against
real production output on those two fields; this is a limit of what
production stores today, not a gap in this schema — `ReferenceTrade` still
carries `exit_rule_index`/`level_index` in full, so the matching module
loses nothing it could otherwise have, and gains full attribution the
moment production's own persistence is extended (which is not this
document's concern).

**A `(symbol, entry_date, exit_date, side)` key is not always unique, and
raw occurrence-order pairing within a colliding key is not a sufficient
resolution on its own** — this document does not endorse that as the
matching module's answer; the concrete algorithm is that later step's
design to make, not this document's, but the risk below is real enough that
it should not be discovered late. On an intraday timeframe, one symbol can
complete more than one same-side round trip within a single calendar day —
`entry_date`/`exit_date`'s `[:10]` truncation (§3) collapses those to the
same key. This schema does carry a bar index (`entry_bar`/`exit_bar`, §3)
but it is no help here: production's `TradeRecord` carries no bar-level
field at all, so there is no production-side counterpart for the matching
module to compare it against — the discriminator has to come from
somewhere both ledgers actually have. `trade_num` (§3's field, mirroring
production's own same-named, same-semantics field) is the discriminator for
that case: both ledgers assign it as a single run-wide monotonic sequence in
emission order, so the matching module needs same-day same-symbol-and-side
trades resolved by their relative occurrence order within that shared key,
not assumed to be 1:1 on the key alone. That resolution is **not** as simple
as pairing by raw occurrence index, though: if production is missing a
trade this module has (e.g. a spec-compilation bug drops an entry, or the
divergence this whole reference ledger exists to catch), naive index-based
pairing misaligns every trade after the gap — reference trade 1 would pair
with production trade 2, and so on — and `trade_num` itself shifts after
any earlier unmatched trade, so it cannot repair the misalignment on its
own. Resolving same-day same-symbol-and-side collisions in the presence of
a possible missing or extra trade (an insertion/deletion, not just a
reordering) is a sequence-alignment problem the matching module must solve
explicitly — e.g. aligning by closest `entry_price`/`exit_price`/`qty`
match within the colliding group, or an edit-distance-style algorithm.

## 7. Implementation status

`simulate(spec, bars, *, entry_slippage_bps=0.0)`
(`strategy_lab/executor/reference_simulator.py`) implements this design's
`ReferenceTrade` record and joins the entry-side and exit-side replays into
complete trade records over a full backtest window, with the following
deviations from this document, all intentional and all pending later work:

- **`qty` is always the nominal `1.0`.** None of §5's sizing formulas
  (`FixedFractionSizing`/`FixedNotionalSizing`/`VolatilityTargetSizing`, the
  ATR resolution order, the `max_position_pct` clamp, whole-share handling),
  the risk-limit admission gates (`max_open_positions`/
  `max_gross_leverage`/`max_symbol_concentration_pct`), or the capital/equity
  ledger are implemented yet. `1.0` is the same nominal quantity
  `RestingStopLoss`/`RestingTakeProfitFamily` already use internally, and it
  satisfies `ReferenceTrade`'s own `qty > 0` invariant. A later matching
  module must not expect `ReferenceTrade.qty` to match production's real,
  sized `TradeRecord.shares` until this layer lands.
- **`simulate()` omits the `starting_equity` parameter.** This document's §2
  signature includes it; with sizing unimplemented (previous bullet), the
  parameter would have no use, and accepting-but-ignoring it would be
  misleading. It will be added back once sizing is built.
- **`oco_bracket` is out of scope and rejected outright.** `simulate()`
  raises `ValueError` for any spec whose working exit rules contain an
  `OcoBracketRule`, rather than silently ignoring it (which would produce
  trades with a missing exit) or modelling only one leg. §5's `oco_bracket`
  subsection remains the design for whichever later step implements both
  legs (OCO sibling cancellation, the same-bar double-touch tie-break where
  the stop leg wins).
- **No reference-side analogue of `open_position_entry_reasons`.** A
  position still open when its symbol's bars run out — including one
  holding only a partially-reduced `scaled_take_profit` remainder —
  produces no `ReferenceTrade`, matching §2's own postcondition, but this
  module has no list to populate the way `TradingServiceResult` does; a
  caller that needs to know *why* an entry rule never closed cannot get that
  from this module today.
- **Cross-symbol processing is independent walks plus a final sort, not a
  genuinely merged timeline.** §2's "Cross-symbol processing order"
  subsection mandates a merged `(timestamp, symbol)` walk because equity
  tracking couples symbols together. With sizing/capital unimplemented
  (first bullet), no state actually couples one symbol's decisions to
  another's, so walking each symbol independently and then stably sorting
  every emitted trade by `(exit bar timestamp, symbol)` before assigning
  `trade_num` produces an IDENTICAL result to a genuinely merged walk, at
  lower implementation cost. This equivalence ends the moment sizing lands:
  a real walk will then need to interleave symbols bar by bar so each
  entry's sizing decision sees every other open position's latest
  mark-to-market value, not merely to keep trades in the right order.
