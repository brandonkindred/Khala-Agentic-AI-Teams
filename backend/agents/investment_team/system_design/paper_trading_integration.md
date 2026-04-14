# Paper Trading Integration

Paper trading is an automatic step in the Strategy Lab cycle: every
**winning** strategy is paper-traded against recent market data before the
cycle marks `complete`. Losing strategies never reach this step, so
paper-trade LLM budget is only spent on candidates that already cleared
the backtest quality bar.

## Winner gate

Paper trading only runs when the backtest flags the strategy as
winning:

```python
is_winning = result.annualized_return_pct > 8.0
```

If `is_winning` is False, the cycle records:

- `paper_trading_status = "skipped"`
- `paper_trading_skipped_reason = "not_winning"`

and proceeds directly to `complete`. This is the explicit contract the
user requested: a strategy that fails backtesting must not proceed to
paper trading.

## Configuration

`RunStrategyLabRequest` ([`api/main.py`](../api/main.py)) accepts two
paper-trading-specific fields:

| Field | Default | Meaning |
|---|---|---|
| `paper_trading_enabled` | `true` | Opt-out flag. When `false`, every winning strategy records `paper_trading_status="skipped"` with reason `"disabled"`. |
| `paper_trading_lookback_days` | `365` | Days of recent OHLCV data fetched for paper trading. |

`initial_capital`, `transaction_cost_bps`, and `slippage_bps` are
inherited from the top-level request so paper trading uses the same
execution assumptions as the backtest.

The paper-trading step runs the orchestrator-generated
`StrategyLabRecord.strategy_code` through `PaperTradingAgent`'s subprocess
sandbox — the same runner used by the backtest — so there is no
LLM-per-bar evaluation cost to bound. If the orchestrator did not
produce strategy code (e.g. the refinement loop exhausted without a
compilable strategy), the step is skipped with reason
`"no_strategy_code"`.

## Record linkage

After a successful paper-trading step, the cycle stores the following
fields on the final `StrategyLabRecord`:

- `paper_trading_session_id` → look up via `GET /strategy-lab/paper-trade/{session_id}`
- `paper_trading_status = "completed"`
- `paper_trading_verdict` (`"ready_for_live"` | `"not_performant"`)

The full `PaperTradingSession` (including every `TradeRecord`,
decisions, metrics, comparison, and divergence analysis) continues to
live in the `_paper_trading_sessions` persistent store.

## Failure contract

The paper-trading step is **not allowed to fail the cycle.** The
backtest winner is already a valuable artifact, so the cycle catches
every exception raised by the step and records it:

| Failure mode | Record state |
|---|---|
| Market data unavailable | `paper_trading_status = "skipped"`, `paper_trading_skipped_reason = "no_market_data"` |
| Orchestrator produced no strategy code | `paper_trading_status = "skipped"`, `paper_trading_skipped_reason = "no_strategy_code"` |
| Any other exception (e.g. sandbox crash) | `paper_trading_status = "failed"`, `paper_trading_error = "<exception text, truncated>"` |

Either way, the `StrategyLabRecord` persists with `is_winning=True` so
the user can re-run paper trading manually via
`POST /strategy-lab/paper-trade` once the underlying issue is resolved.

## SSE phase events

The worker emits these phase events during the step (see
[`strategy_lab_pipeline.md`](./strategy_lab_pipeline.md) for the full
list):

- `paper_trading` — entering the step
- `paper_trading_complete` — session finished, includes verdict and trade count
- `paper_trading_skipped` — step did not run, includes reason
- `paper_trading_failed` — step raised an exception, includes truncated detail

## Standalone endpoint still available

`POST /strategy-lab/paper-trade` is intentionally preserved. Use it to:

- Retry paper trading on a record that originally recorded
  `paper_trading_status="skipped"` (reason `"no_market_data"` or
  `"no_strategy_code"`) or `paper_trading_status="failed"`.
- Run a second paper-trading session against a winning record with
  different parameters (e.g. a longer `lookback_days`).

The cycle-level `paper_trading_session_id` continues to point at the
session produced during the cycle; each manual invocation writes an
additional `PaperTradingSession`.
