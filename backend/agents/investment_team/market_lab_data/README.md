# Market lab data (Strategy Lab)

Free-tier `StrategyLabMarketDataProvider` implementations live here. The default is **`FreeTierMarketDataProvider`** (Frankfurter FX, optional FRED `DGS10` when `FRED_API_KEY` is set, CoinGecko simple prices). Results are wrapped in **`MarketLabContext`** for prompts and logging.

## Extension points

- Implement `StrategyLabMarketDataProvider` (see `provider.py`) with `fetch_context(request: StrategyLabDataRequest) -> MarketLabContext`.
- Register via `STRATEGY_LAB_MARKET_DATA_PROVIDER` (currently only `free_tier` is implemented; unknown values fall back to `free_tier` with a warning).
- **Do not** call HTTP clients directly from new strategy lab agents—inject the same provider instance from `run_strategy_lab` (or a future DI helper) so caching, timeouts, and keys stay consistent.

## Environment

| Variable | Purpose |
|----------|---------|
| `STRATEGY_LAB_MARKET_DATA_FETCH_TIMEOUT_SEC` | Per-fetch wall time budget (default `8`). |
| `STRATEGY_LAB_MARKET_DATA_CACHE_TTL_SEC` | In-process cache TTL for snapshots (default `120`). |
| `FRED_API_KEY` | Optional; enables US 10Y (`DGS10`) in the snapshot. |
| `STRATEGY_LAB_SIGNAL_EXPERT_ENABLED` | `true`/`false` — disables the signal LLM step (ideation still runs). |

Attribution and rate limits for each free API must be respected in production deployments.
