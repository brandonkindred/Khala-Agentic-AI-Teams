# Trade Record Schema

A `TradeRecord` ([`models.py`](../models.py)) represents one simulated
round-trip (entry → exit). The legacy `TradeSimulationEngine` bar-by-bar
evaluator that used to produce these has been retired — strategy code now
runs exclusively through the event-driven `trading_service/` engine
(`FillSimulator`, [`trading_service/engine/fill_simulator.py`](../trading_service/engine/fill_simulator.py)),
which builds the finalized `TradeRecord`s directly; `TradingService` running
that engine is the actual production path for both backtests and paper
trading. The same schema applies to **both** backtest trades (stored in
`BacktestRecord.trades`) and paper-trading trades (stored in
`PaperTradingSession.trades`), so downstream analysis tooling can treat
them uniformly.

## Fields

| Field | Type | Meaning |
|---|---|---|
| `trade_num` | `int` | 1-based sequence number within the session. |
| `entry_date` | `str` | ISO date of the entry bar. |
| `exit_date` | `str` | ISO date of the exit bar (or final bar if force-closed). |
| `symbol` | `str` | Asset the trade was placed on (e.g. `"AAPL"`, `"BTC-USD"`). |
| `side` | `str` | `"long"` or `"short"`. |
| `shares` | `float` | Quantity traded. |
| `position_value` | `float` | `entry_fill_price × shares` (cash committed at entry). |
| `entry_price` | `float` | **Legacy alias** for `entry_fill_price`. Kept for backward compatibility. |
| `exit_price` | `float` | **Legacy alias** for `exit_fill_price`. Kept for backward compatibility. |
| `entry_bid_price` | `float \| None` | Reference price at the entry bar, **before** slippage. Order-type dependent — for a market order this is the bar's **open**, not its close (see "Where it's produced"). |
| `entry_fill_price` | `float \| None` | Actual filled price paid at entry, **after** slippage: `entry_bid_price × (1 + slippage_bps/10000)`, rounded to 4 dp below $10 and 2 dp at or above. |
| `exit_bid_price` | `float \| None` | Reference price at the exit bar, **before** slippage. Not the raw close: on a partially-filled exit this is `weighted_avg_exit_bid_price`, the quantity-weighted mean of the per-slice reference prices. |
| `exit_fill_price` | `float \| None` | Actual filled price received at exit, **after** slippage: `exit_bid_price × (1 − slippage_bps/10000)`, rounded the same way as entry. |
| `entry_order_type` | `str` | Order type used for entry. `market`, `limit`, `stop`, `stop_limit`, and `trailing_stop` are all live and each derives its own reference price. |
| `exit_order_type` | `str` | Order type used for exit — same semantics. |
| `gross_pnl` | `float` | P/L before transaction costs: `shares × (exit_fill - entry_fill)` (sign-flipped for shorts). |
| `net_pnl` | `float` | P/L after transaction costs, charged on entry and exit notional **separately**: `(entry_notional + exit_notional) × cost_bps/10000`. This is the canonical P/L used to drive `outcome`, `cumulative_pnl`, and aggregate metrics. |
| `return_pct` | `float` | Per-trade return in percent, **net** of costs and over entry notional: `net_pnl / (entry_fill_price × shares) × 100`. |
| `hold_days` | `int` | Calendar days between `entry_date` and `exit_date` (floor of 1). |
| `outcome` | `str` | `"win"` if `net_pnl > 0`, else `"loss"`. |
| `cumulative_pnl` | `float` | Running total of `net_pnl` across the session. |

## Bid vs fill — worked example

Backtest config: `slippage_bps = 2`, `transaction_cost_bps = 5`. Simplified for
illustration: uses each bar's close as its reference price throughout. In
production the reference price is order-type-dependent (see "Where it's
produced" below) — a market order's reference price is the bar's *open*, not
its close.

Entry bar has `close = 100.00`. The simulator records:

- `entry_bid_price = 100.00` (the raw close we would see on a quote board)
- `entry_fill_price = 100.00 × (1 + 2/10_000) = 100.02` (what we actually paid)

Exit bar has `close = 105.00`. The simulator records:

- `exit_bid_price = 105.00`
- `exit_fill_price = round(105.00 × (1 − 2/10_000), 2) = 104.98` — the simulator
  rounds each fill before storing it (4 dp under $10, 2 dp at or above), so the
  stored value is not the unrounded `104.979`

With `shares = 10` (using `FillSimulator`'s actual formula — entry and exit
notional charged separately, not a flat notional doubled):

- `gross_pnl = 10 × (104.98 − 100.02) = 49.60`
- `entry_notional = 100.02 × 10 = 1000.20`; `exit_notional = 104.98 × 10 = 1049.80`
- `tx_cost = (1000.20 + 1049.80) × (5/10_000) = 1.025`
- `net_pnl = round(49.60 − 1.025, 2) = 48.58`
- `return_pct = round(48.58 / 1000.20 × 100, 2) = 4.86`

## Backward compatibility

All six new fields (`entry_bid_price`, `entry_fill_price`,
`exit_bid_price`, `exit_fill_price`, `entry_order_type`,
`exit_order_type`) are optional with safe defaults. Records persisted
before these fields existed deserialize cleanly:

- The four price fields default to `None`.
- Both order-type fields default to `"market"`.

Legacy `entry_price` / `exit_price` remain populated and continue to
equal the fill prices. New analysis code should prefer the explicit
`*_bid_price` / `*_fill_price` fields; existing consumers work unchanged.

## Where it's produced

The production path — both sandboxed backtests and paper trading — is
[`trading_service/engine/fill_simulator.py::FillSimulator`](../trading_service/engine/fill_simulator.py),
the live event-driven fill engine driven by `TradingService`.
[`Position`](../trading_service/engine/portfolio.py) (`trading_service/engine/portfolio.py`,
`FillSimulator`'s own state carrier) carries `entry_bid_price` and
`entry_order_type` from the entry bar through to the close. `exit_bid_price`
is **not** simply the exit bar's raw close: `execution_model.py` derives an
order-type-dependent reference price (the bar open for market orders;
limit/stop/stop-limit orders use their own reference-price logic against
that bar), and when an exit fills across multiple partial slices,
`FillSimulator` stores `Position.weighted_avg_exit_bid_price` — the
quantity-weighted average of those per-slice reference prices — as the final
`exit_bid_price`. Slippage is applied on both sides symmetrically on top of
that reference price. `trade_simulator.OpenPosition` is a separate, retired-simulator
dataclass kept only for legacy/unit-test consumers — not the production state
carrier. Transaction costs are charged on entry and exit notional
separately: `(entry_notional + exit_notional) * cost_rate`.

**`trade_simulator.py` is not dead code**, despite `TradeSimulationEngine` and
`OpenPosition` being retired: the module still owns `compute_metrics`, the
canonical P&L / Sharpe / drawdown estimator imported by
`strategy_lab/orchestrator.py`, `strategy_lab/zero_trade_repair.py`, and
`trading_service/modes/backtest.py`. Retiring the file wholesale would break
metrics for every backtest.

[`strategy_lab/executor/trade_builder.py::build_trade_records`](../strategy_lab/executor/trade_builder.py)
is a separate, older raw-trade-dict-to-`TradeRecord` converter with only one
remaining test caller — it is **not** part of the current production
execution path (`trading_service/modes/sandbox_compat.py`'s own docstring
notes `FillSimulator` makes it unnecessary there), and its cost math is not
equivalent to `FillSimulator`'s: it charges a flat `position_value * cost_mult
* 2` rather than summing entry and exit notional separately. Don't treat it
as a parity reference for either the execution path or the cost formula.
