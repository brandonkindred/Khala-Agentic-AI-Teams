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
writing, only the OCO bracket's take-profit leg and any `style="limit"` stop
already rest at their authored price in production — standalone
`stop_loss`/`take_profit`/`scaled_take_profit` with `style="market"` still
fire via bar-close detection and close at the *next* bar's market open, the
same next-bar-open approximation `signal_exit` uses. This document
deliberately models the *target* resting-order behavior a separate,
in-flight execution-fidelity change will ship for those standalone rules
(exact level, or worse-of-open-and-level on a gap, filled on the trigger bar
itself) — because once that behavior ships, comparing the reference ledger
against the pre-change approximation would make every stop/take-profit trade
trivially "diverge." Implementers should not be surprised that this document
does not match the current `_build_close_order` behavior for those three
rule kinds; that divergence is intentional and temporary.

It is **not** a fill-cost engine. Explicitly out of scope:

- **Slippage and transaction costs.** Reference prices are exact bar-derived
  levels (a resting order's authored price, or worse-of-open-and-level on a
  gap), not slippage-adjusted fills. See §3 for exactly which production
  field this corresponds to.
- **Order-book / partial-fill mechanics.** No order queue, no
  participation-cap clipping, no multi-slice fills. One trigger, one fill,
  at a single price.
- **Cost-aware position sizing.** Entry quantity is resolved from
  `spec.sizing` against a running equity figure this module tracks itself
  (seeded from the `starting_equity` input, marked to market from this
  module's own no-slippage, no-cost reference prices only — see §5's
  "Entries" subsection) — not against the true, cost-adjusted equity a real
  backtest or paper-trading run would show. A ladder rung's own quantity is
  whatever its `qty_fraction * original_qty` implies once the entry quantity
  is known.

## 2. Module boundary

```python
def simulate(
    spec: StrategySpec,
    bars: Mapping[str, Sequence[Bar]],
    starting_equity: float,
) -> List[ReferenceTrade]:
    """Pure re-simulation of spec.entry_rules / spec.exit_rules over bars.
    """
```

- `spec` is the existing `StrategySpec` Pydantic model (`models.py`) — reused
  verbatim, no translation layer.
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
resolution (§5's "Entries" subsection covers both of the latter two). Today
this logic lives inside the production fill engine and dispatchers, which
this module must not depend on (see below) — so it is modeled here at the
semantic level described in §5, as new pure code, not imported from the
production engine.

### Exclusions

This module must not import, directly or transitively:

- `trading_service/service.py` (the live dispatchers and their engine-exit
  reason-string constant),
- `trading_service/engine/fill_simulator.py` (the order book, pending-order
  state machine, trailing-stop and stop-limit mechanics),
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

### Contract

**Preconditions:**
- `spec` is a validated `StrategySpec` (Pydantic's own validators already
  enforce its internal invariants — e.g. at most one `OcoBracketRule`,
  strictly increasing ladder `pct` values).
- For every symbol `spec` references, `bars[symbol]` is non-empty and
  strictly increasing by `timestamp`.
- `starting_equity > 0`.

**Postconditions:**
- The returned list is ordered by non-decreasing `entry_bar` within each
  symbol.
- Every `ReferenceTrade` satisfies `0 <= entry_bar <= exit_bar <
  len(bars[symbol])`.
- Each fully closed position produces exactly one `ReferenceTrade` — a
  position reduced by prior `scaled_take_profit` rungs aggregates them into
  a single row (§5's "Exit aggregation" subsection) rather than emitting one
  per rung.
- A position still open at `bars[symbol]`'s last bar produces no
  `ReferenceTrade` for that position.
- No forbidden import (§2 Exclusions) occurs as a result of calling
  `simulate`.

**Invariants:**
- `simulate` has no side effects: it does not mutate `spec` or `bars`, and
  performs no I/O.
- `simulate` is deterministic — identical `(spec, bars, starting_equity)`
  inputs always produce an identical output list. This is required for it
  to function as a reference oracle; a non-deterministic simulator cannot be
  diffed meaningfully against a single production run.

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
| `entry_bar` | `int` | *(none — new)* | Index into `bars[symbol]` where the position opened — one bar after the entry rule's trigger bar (see §5's "Entries" subsection). Primary key for this module's own bookkeeping (ladder rungs, per-position running state). |
| `exit_bar` | `int` | *(none — new)* | Index into `bars[symbol]` where this trade (or rung) closed. |
| `entry_date` | `str` | `entry_date` | `bars[symbol][entry_bar].timestamp[:10]` — truncated to the date portion exactly as production does (`pos.entry_timestamp[:10]`), so an intraday `Bar.timestamp` still matches production's date-only comparison key. |
| `exit_date` | `str` | `exit_date` | `bars[symbol][exit_bar].timestamp[:10]`, truncated the same way (`bar.timestamp[:10]` in `_fill_exit`). |
| `entry_price` | `float` | `entry_bid_price` | Pre-slippage reference level — **not** `entry_fill_price` or the legacy `entry_price` alias (both are post-slippage in production). See rationale below. |
| `exit_price` | `float` | `exit_bid_price` | Pre-slippage reference level. For a position closed in one shot this is that close's reference price; for a position that passed through one or more `scaled_take_profit` rungs first, this is the quantity-weighted average across every partial exit and the final close (§5's "Exit aggregation" subsection), mirroring `pos.weighted_avg_exit_price`. |
| `qty` | `float` | `shares` | Equals the position's entry quantity (`original_qty`), **not** the remaining size after any partial rungs — mirrors production's `TradeRecord.shares = pos.original_qty`. Entry quantity resolution is specified in §5's "Entries" subsection. |
| `exit_rule_kind` | `str` | *(none — derived from `exit_reason` today)* | One of the values in §4's vocabulary table, always populated — every closed position in this model closes via some exit rule (there is no strategy-emitted arbitrary close path here). Describes only the position's final closing event (§5's "Exit aggregation"), not any earlier partial rung. |
| `exit_rule_index` | `int` | *(none)* | Which `spec.exit_rules[i]` fired the final close — mirrors `ExitIntent.rule_index`. |
| `level_index` | `Optional[int]` | *(none)* | Set only when the position's final closing event was itself a `scaled_take_profit` rung, identifying which rung — mirrors `ExitIntent.level_index`. `None` whenever some other rule kind performed the final close, even if earlier rungs fired first. |

**Why `entry_price`/`exit_price` map to the bid fields, not the fill fields:**
production's `entry_fill_price`/`exit_fill_price` (and their legacy
`entry_price`/`exit_price` aliases) already have slippage baked in
(`entry_bid_price × (1 ± total_slip_bps / 10_000)`, per
[`trade_record_schema.md`](./trade_record_schema.md)). Since this module
explicitly excludes slippage/cost modeling (§1), its own `entry_price`/
`exit_price` are the pre-slippage reference levels — directly comparable to
production's `entry_bid_price`/`exit_bid_price`, not the fill prices.

**Why both a bar index and a date string:** `entry_bar`/`exit_bar` are this
module's own primary keys — needed internally to track ladder-rung
sequencing and per-position running state (trailing watermarks, stop-limit
arm/latch) across the `bars` sequence. `entry_date`/`exit_date` exist because
they are what a `TradeRecord` actually has; the later matching module keys
its trade-to-trade comparison off `(symbol, entry_date, exit_date, side)`-
style fields, not bar indices, since production carries no bar index at all.
Carrying both from the start means the matching module never has to derive
one from the other.

**Deliberately excluded** (all downstream cost/execution-mechanics fields,
out of scope per §1): `position_value`, `gross_pnl`, `net_pnl`, `return_pct`,
`outcome`, `cumulative_pnl`, `entry_fill_price`, `exit_fill_price`,
`entry_order_type`, `exit_order_type`, `participation_clipped`,
`partial_fill_count`, `total_unfilled_qty`, `hold_days` (trivially derivable
by the matching module from the two dates), `entry_reason`/`exit_reason`
free text (superseded by the structured `exit_rule_kind`/`exit_rule_index`/
`level_index` triple).

### Invariants (as a value object)

- `entry_bar <= exit_bar`.
- `qty > 0`.
- `entry_price > 0` and `exit_price > 0`.
- `side in ("long", "short")`.
- `exit_rule_kind` and `exit_rule_index` are always populated (every emitted
  `ReferenceTrade` represents a fully closed position, and full closure
  always happens via some exit rule firing).
- `level_index is not None` implies `exit_rule_kind == "scaled_take_profit"`.

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

**Suppression while a position is open or pending.** An entry rule that
keeps matching on later bars while the symbol already has an open position,
or already has an entry queued from a prior bar's trigger not yet filled,
must **not** open a second, overlapping position — mirrors
`_EngineEntryDispatcher.maybe_emit`'s two gates (`portfolio.positions.get(sym)
is not None` and an already-queued same-symbol entry). Without this, a
predicate that stays true for several consecutive bars would open one
production position but many overlapping reference ones.

**Quantity.** `simulate` resolves each entry's quantity from `spec.sizing`
against a running equity figure it tracks itself — seeded at
`starting_equity`, then marked to market at each entry-sizing decision using
the **latest observed price of every currently open position across all
symbols** (unrealized value included, not just realized/closed reference
trades) — mirroring `Portfolio.mark_to_market()`, which values open
positions the same way, and consistent with §1's scope in that this
mark-to-market uses only this module's own no-slippage reference prices,
never the true, cost-adjusted equity a real run would show. This is why the
"Cross-symbol processing order" note above matters: computing this equity
figure correctly for a multi-symbol spec requires walking every symbol's
bars in one merged chronological order, not resolving one symbol's trades
in isolation. The per-sizing-kind formula mirrors production's (evaluated
against the trigger bar's close, the same reference price used for the
reference fill's next-bar-open resolution one bar later):

- `FixedFractionSizing`: `equity * fraction / trigger_bar.close`.
- `FixedNotionalSizing`: `notional_usd / trigger_bar.close`.
- `VolatilityTargetSizing`: `equity * target_annual_vol / (trigger_bar.close
  * atr)`, where `atr` comes from the same indicator view this module
  already needs for `signal_exit` predicates (see §2's `PandasHistoryView`
  dependency note) — reinforcing that dependency rather than introducing a
  new one. Which ATR: scan the entry rules' own predicates first, then the
  signal-exit rules' predicates, for the first ATR indicator reference (so a
  spec-configured ATR period is honored); fall back to a default ATR(14)
  reference when no rule references one. Warmup fallback: if the resolved
  ATR is unavailable or non-positive at the trigger bar (not enough history
  yet), fall back to a one-share probe instead of failing, then still run it
  through the whole-share/`max_position_pct` handling below.

**Position-cap clamp, applied first.** Before any whole-share handling, the
raw quantity from every sizing kind above — not just a sub-1 result — is
clamped so its notional does not exceed `equity * max_position_pct / 100`
at the trigger bar's close: `qty = min(raw_qty, equity * max_position_pct /
100 / trigger_bar.close)`, mirroring `_cap_qty_to_position`. This applies
unconditionally to all three sizing kinds this module models (fixed-fraction,
fixed-notional, volatility-target) — a `FixedNotionalSizing`
or `VolatilityTargetSizing` result above the cap must be reduced, not passed
through uncapped.

Whole-share handling, applied after the clamp above, mirrors production's
cap-aware floor rather than a blanket skip: a clamped quantity `>= 1` floors
down to `int(qty)`. A clamped quantity `< 1` on a non-fractional asset class
is **promoted to one share** if that one share still satisfies
`max_position_pct` (re-checked at exactly one share, since flooring up can
itself re-breach the cap), and only **skipped** (no entry) if even one
share would breach it. A fractional-capable asset class (crypto/forex)
instead keeps the raw, uncapped-floor quantity as-is (dropped to zero only
if the cap itself drove it to zero or below).

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
`pos.weighted_avg_exit_price`. Concretely, for that one emitted record:

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

A position still open at `bars[symbol]`'s last bar — including one holding
only a partially-reduced remainder from earlier rungs — produces **no**
`ReferenceTrade` at all, matching production's `open_position_entry_reasons`
handling rather than a synthetic force-close.

### Per-bar evaluation order

Two ordering rules this module must enforce, both to stay behaviorally
identical to production, not just directionally similar:

- **Queued fills resolve before this bar's own rule evaluation.** An entry
  or `signal_exit` triggered on the prior bar fills at *this* bar's open
  first; only after that does this bar's own resting-order/bar-close
  evaluation run (stop/take-profit/scaled-rung triggers, bracket-leg
  reachability, this bar's own entry/signal-exit predicate checks). A
  position a queued fill closes at this bar's open is gone before any other
  rule on this same bar gets a chance to fire against it — mirrors
  production's per-bar step order (submit-and-fill the previous bar's queued
  orders, *then* evaluate this bar's rules).
- **No exit rule is eligible on a position's own `entry_bar`.** Every exit
  rule kind — `stop_loss`/`take_profit`/`scaled_take_profit`/`signal_exit`
  evaluation, and bracket-leg reachability alike — first becomes eligible at
  `entry_bar + 1`, never on `entry_bar` itself, regardless of what that
  bar's own range would otherwise trigger. This unifies two production
  mechanisms this module doesn't import but must reproduce the effect of:
  the dispatcher's `just_opened` gate (skips all bar-by-bar rule evaluation
  on the bar a position first appears) and the bracket-child submission
  guard (a bracket's children are stamped with the entry-fill bar's own
  timestamp and are skipped whenever their submission bar isn't strictly
  earlier than the bar being evaluated).

### `stop_loss`

Covers all `basis` × `style` combinations from `StopLossRule`. Unlike a
bracket leg (see `oco_bracket` below), a standalone stop/take-profit's
`basis="entry_price"` level is anchored to the position's actual
`ReferenceTrade.entry_price` (the next-bar-open fill from the "Entries"
subsection above) — these rules evaluate against the live position after it
has actually filled, not against a signal-time reference resolved before
fill.

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
  not itself carry this history. **Evaluate-then-extend ordering matters**:
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
(`entry_price * (1 ± pct)`). Always fills at the exact target — a limit
order's defining property is that it never fills worse than its price, so
unlike `stop_loss` there is no worse-of-open-and-level adjustment here, even
on a gap through the target. Fill bar is the trigger bar.

### `scaled_take_profit`

A ladder of resting limit orders, one per `TakeProfitLevel`, each at
`entry_price * (1 ± level.pct)`. Each rung's quantity is
`level.qty_fraction * original_qty`, fixed at entry — not a fraction of the
live (already-reduced) position size. Sequencing mirrors the production
ladder cursor's per-rule-index "next un-fired rung" counter: only the
lowest un-fired rung is eligible to trigger on a given bar, and a single bar
advances the cursor by exactly one rung even if the bar's range would have
cleared several rungs at once — this module must maintain that same
one-rung-per-position-per-bar advancement rule, matching the counter's
semantics rather than firing every technically-reachable rung in one step.
Fill price for a firing rung follows the same exact-price rule as
standalone `take_profit`; fill bar is the trigger bar. A fired rung does
**not** emit its own `ReferenceTrade` — see the "Exit aggregation"
subsection above for how rungs feed into the single record eventually
emitted when the position is fully closed.

### `signal_exit`

Unchanged from current (and post-resting-exit-epic) engine semantics: a
bar-close predicate decision, filled at the **next** bar's open. This is the
one exit kind where the fill bar differs from the trigger bar —
`exit_bar = trigger_bar + 1` — unlike every resting-order kind above, where
trigger bar and fill bar coincide. If the predicate fires on the final bar
of `bars`, there is no next bar to fill on; this module treats that as "no
trade emitted for this trigger" rather than fabricating a fill past the end
of the data.

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
