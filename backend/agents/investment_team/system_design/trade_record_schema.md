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
| `entry_bid_price` | `float \| None` | Reference close price at the entry bar, **before** slippage adjustment. |
| `entry_fill_price` | `float \| None` | Actual filled price paid at entry, **after** slippage: `entry_bid_price × (1 + slippage_bps/10000)`. |
| `exit_bid_price` | `float \| None` | Reference close price at the exit bar, **before** slippage. |
| `exit_fill_price` | `float \| None` | Actual filled price received at exit, **after** slippage: `exit_bid_price × (1 − slippage_bps/10000)`. |
| `entry_order_type` | `str` | Order type used for entry — `"market"` today; field is forward-compatible with `"limit"`, `"stop"`, etc. |
| `exit_order_type` | `str` | Order type used for exit — same semantics. |
| `gross_pnl` | `float` | P/L before transaction costs: `shares × (exit_fill - entry_fill)` (sign-flipped for shorts). |
| `net_pnl` | `float` | P/L after transaction costs (round-trip `cost_bps` applied to `position_value`). This is the canonical P/L used to drive `outcome`, `cumulative_pnl`, and aggregate metrics. |
| `return_pct` | `float` | Per-trade return in percent: `(exit_fill - entry_fill) / entry_fill × 100` (sign-flipped for shorts). |
| `hold_days` | `int` | Calendar days between `entry_date` and `exit_date` (floor of 1). |
| `outcome` | `str` | `"win"` if `net_pnl > 0`, else `"loss"`. |
| `cumulative_pnl` | `float` | Running total of `net_pnl` across the session. |

## Bid vs fill — worked example

Backtest config: `slippage_bps = 2`, `transaction_cost_bps = 5`.

Entry bar has `close = 100.00`. The simulator records:

- `entry_bid_price = 100.00` (the raw close we would see on a quote board)
- `entry_fill_price = 100.00 × (1 + 2/10_000) = 100.02` (what we actually paid)

Exit bar has `close = 105.00`. The simulator records:

- `exit_bid_price = 105.00`
- `exit_fill_price = 105.00 × (1 − 2/10_000) = 104.979`

With `shares = 10` (using `FillSimulator`'s actual formula — entry and exit
notional charged separately, not a flat notional doubled):

- `gross_pnl = 10 × (104.979 − 100.02) = 49.59`
- `entry_notional = 100.02 × 10 = 1000.20`; `exit_notional = 104.979 × 10 = 1049.79`
- `tx_cost = (1000.20 + 1049.79) × (5/10_000) ≈ 1.02`
- `net_pnl = 49.59 − 1.02 ≈ 48.57`

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
`entry_order_type` from the entry bar through to the close; the exit bar's
raw close becomes `exit_bid_price`, and slippage is applied on both sides
symmetrically. `trade_simulator.OpenPosition` is a separate, retired-simulator
dataclass kept only for legacy/unit-test consumers — not the production state
carrier. Transaction costs are charged on entry and exit notional
separately: `(entry_notional + exit_notional) * cost_rate`.

[`strategy_lab/executor/trade_builder.py::build_trade_records`](../strategy_lab/executor/trade_builder.py)
is a separate, older raw-trade-dict-to-`TradeRecord` converter with only one
remaining test caller — it is **not** part of the current production
execution path (`trading_service/modes/sandbox_compat.py`'s own docstring
notes `FillSimulator` makes it unnecessary there), and its cost math is not
equivalent to `FillSimulator`'s: it charges a flat `position_value * cost_mult
* 2` rather than summing entry and exit notional separately. Don't treat it
as a parity reference for either the execution path or the cost formula.
