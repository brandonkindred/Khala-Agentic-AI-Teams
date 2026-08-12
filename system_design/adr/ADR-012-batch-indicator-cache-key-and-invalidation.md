# ADR-012 — Batch indicator cache: key composition and invalidation contract

- **Status**: Proposed
- **Date**: 2026-08-12
- **Owner**: Investment Team / Strategy Lab
- **Related**:
  - Foundation design-decision deliverable for the effort to add a cross-strategy,
    batch-scoped indicator-value cache to Strategy Lab's batch backtests — the rest of
    that effort's sub-issues (the `BatchIndicatorCache` implementation, its wiring into
    `IndicatorRegistry`, and its construction/sharing inside the batch Temporal
    workflow) depend on this contract.
  - `backend/agents/investment_team/strategy_lab/indicators/streaming.py` —
    `IndicatorRegistry`, the existing per-instance indicator cache this ADR reconciles
    with.
  - `backend/agents/investment_team/strategy_lab/backtest_cache.py` — `BacktestCache`,
    the existing per-design-attempt whole-result cache this ADR's key/invalidation
    scheme is modeled on.
  - `backend/agents/investment_team/market_data_cache/store.py` —
    `compute_dataset_fingerprint`, reused below as the date-range/data-version
    fingerprint primitive.
  - `backend/agents/investment_team/strategy_lab/temporal/workflows.py` —
    `StrategyLabBatchWorkflow` / `StrategyLabCycleWorkflow`, the batch/wave fan-out this
    ADR's concurrency requirement is written against.

## Context

A Strategy Lab batch backtest evaluates many candidate strategies ("cycles") against
the same asset-class universe, timeframe, and date range in one batch run
(`StrategyLabBatchWorkflow`, `strategy_lab/temporal/workflows.py`). Each cycle runs as
an independent, concurrently-scheduled `StrategyLabCycleWorkflow` child workflow, and
each cycle's design attempt computes its own indicator series from scratch — even when
two different candidate strategies both use, say, `SMA(50)` on `AAPL`/`1d` over the
same date range. Nothing today shares that computation across cycles in the same
batch, so structurally identical indicator work is repeated once per candidate.

Two caches already exist in Strategy Lab, and neither covers this gap:

- **`IndicatorRegistry`** (`strategy_lab/indicators/streaming.py`) is a **per-instance**
  cache — one registry per strategy / per `StreamingHistoryView` — that memoizes a
  single bar stream's incremental indicator walk (same-bar / expand / slide
  detection via `_bar_fingerprint`/`_advance_kind`). It never outlives one strategy's
  execution and was never intended to share work across strategies.
- **`BacktestCache`** (`strategy_lab/backtest_cache.py`) memoizes *whole*
  `run_strategy_code` results, but is scoped to a single design attempt and keyed on
  the full `(code, market_data, config, spec)` tuple — a cache hit requires the entire
  strategy to be identical, not just one shared indicator inside it.

This ADR defines the cache-key strategy and invalidation contract for a new
**batch-scoped indicator-value cache** that sits between these two: narrower than
`BacktestCache` (keys on one indicator computation, not a whole strategy) and broader
than `IndicatorRegistry` (shared across every cycle in a batch, not just one bar
stream). It is a documentation/design decision only — no code changes accompany this
ADR; the cache implementation, its `IndicatorRegistry` integration, and its batch
workflow wiring are separate follow-on work built against this contract.

## Decision

### Cache-key composition

A cache entry's key identifies one indicator computation as the tuple:

```
(indicator_name, canonical_params, symbol, timeframe, data_fingerprint)
```

- **`indicator_name`** — the registered indicator's name, e.g. `"sma"`, `"macd"`
  (`strategy_lab/executor/indicators.py`'s `INDICATORS` table is the authoritative list
  of names).
- **`canonical_params`** — every parameter that steers the indicator's math: `period`,
  `source`, and indicator-specific args (`fast`/`slow`/`signal` for MACD, `num_std` for
  Bollinger Bands, `atr_period`/`multiplier` for Keltner, etc.). This is the *same*
  identity information `IndicatorRegistry` already keys on per-method (its tuples are
  `(name, period, source)`, `(name, symbol, fast, slow, signal, source)`, and so on) —
  a batch-cache key can be derived directly from the same arguments a registry call
  already carries, with no new parameter surface to invent. Float params (`num_std`,
  `multiplier`) must be canonicalized (fixed serialization, e.g. via `repr` or
  round-tripped JSON) before hashing so equal values always hash identically.
- **`symbol`** — always included, unconditionally (see Reconciliation below for why
  this differs from `IndicatorRegistry`'s inconsistent per-method inclusion).
- **`timeframe`** — the bar interval (`"1d"`, etc.) the series was computed over.
- **`data_fingerprint`** — a fingerprint of the exact OHLCV data the indicator was
  computed against, reusing `compute_dataset_fingerprint`
  (`market_data_cache/store.py:220`) scoped to the single symbol's bar slice (i.e. the
  per-symbol `_hash_bars` leg that function already computes internally, not the
  symbol-order-independent multi-symbol combination). This stands in for the "date
  range" component named in the epic: the bars handed to the indicator computation
  already encode the exact start/end dates, any gaps, and any adjustments, so hashing
  the bars is a strictly more precise identity than hashing a `(start_date, end_date)`
  pair would be — two runs with the same nominal date range but different data (a
  restated bar, a newly-backfilled gap) must not collide, and a content hash catches
  that where a date-range string cannot.

The five components are composed into a single cache key by hashing them together with
SHA-256, mirroring `BacktestCache._key`'s pattern of digesting canonicalized components
in a fixed order (`hash_code` + market-data fingerprint + config hash + spec hash,
`backtest_cache.py:164-176`):

```
digest = sha256(
    indicator_name.encode() + b"\x00" +
    canonical_params_json.encode() + b"\x00" +
    symbol.encode() + b"\x00" +
    timeframe.encode() + b"\x00" +
    data_fingerprint.encode()
)
```

A plain hashable tuple `(indicator_name, canonical_params_tuple, symbol, timeframe,
data_fingerprint)` would also work as a dict key and avoids hashing overhead, but a
digest is preferred for consistency with `BacktestCache`'s established convention in
this codebase, a uniform fixed-size key regardless of how many params an indicator
takes, and no risk of an uncanonicalized float param breaking tuple equality.

### Invalidation rules

- **The key is the invalidation boundary.** Any difference in indicator name, params,
  symbol, timeframe, or underlying data content produces a different key, so a stale
  read is structurally impossible as long as every input that affects the computed
  series is represented in the key. This is deliberately conservative in the same
  spirit as `BacktestCache._config_hash`'s choice to hash the whole config rather than
  a hand-picked subset (`backtest_cache.py:110-113`): over-including a component only
  lowers the hit rate, it can never serve a value computed under different assumptions.
- **Structural invariant to preserve**: every indicator registered in
  `executor/indicators.py`'s `INDICATORS` table must compute its output from *only*
  `(params, symbol, timeframe, data)` — no indicator may read hidden state (wall-clock
  time, a live external call, prior-strategy context) that isn't captured by the key.
  This holds for all indicators today; any future indicator that would violate it must
  either gain a new key component or bypass the cache entirely.
- **Custom-code bypass**: strategies with `requires_custom_code=True` whose `on_bar`
  logic computes indicator-like values outside the registered `IndicatorSpec` surface
  (arbitrary Python, not a call the batch cache can recognize by name+params) cannot be
  keyed by this scheme and must bypass the cache — the same posture
  `BacktestCache._is_nondeterministic` takes toward code the cache cannot safely
  memoize (`backtest_cache.py:54-78`): decline to cache rather than risk a wrong hit.
- **Scope and lifetime**: the cache is batch-scoped only. It must be constructed fresh
  when a batch begins and discarded when the batch ends — never reused across batches
  or across unrelated runs — mirroring `BacktestCache`'s per-design-attempt discipline
  (`backtest_cache.py:15-18`) of never crossing a market-data snapshot boundary. A
  batch-scoped cache can only ever be invalidated by ceasing to exist; it never needs
  active eviction within its lifetime because every entry is content-addressed.
- **Concurrency requirement**: a batch's cycles run as independent, concurrently
  scheduled `StrategyLabCycleWorkflow` child workflows
  (`strategy_lab/temporal/workflows.py`, `max_parallel`-bounded fan-out), so the cache
  must tolerate concurrent reads and concurrent writes-on-miss without corrupting
  state. Two cycles racing to compute the same key and both writing their
  (necessarily identical) result is acceptable — it costs a redundant computation, not
  correctness. A half-written or torn entry read by a concurrent reader is not
  acceptable. The specific locking or store mechanism (in-process lock, single-writer
  store, compute-then-atomic-set) is left to the implementation issue, but freedom from
  torn reads under concurrent access is a hard requirement on any implementation built
  against this contract.

### Reconciliation with `IndicatorRegistry`

`IndicatorRegistry`'s existing per-method keys already carry most of the identity
information this ADR's key needs — `(name, period, source)` for single-symbol methods
like `sma`/`ema`/`rsi`, `(name, symbol, fast, slow, signal, source)` for
symbol-sensitive methods like `macd`/`donchian`/`keltner`/`obv`
(`indicators/streaming.py:506,529,549,993,1126,1183,1243`) — but that key shape is
**inconsistent by construction**: symbol is included only for the four methods whose
docstrings note a multi-stream hazard (a shared registry seeing more than one symbol's
bars), and never for the rest, because a single `IndicatorRegistry` instance is
documented to normally see one bar stream at a time (class docstring,
`streaming.py:341-365`).

The batch cache does not need to fix or depend on that inconsistency. It is a
different cache at a different layer:

| | `IndicatorRegistry` | Batch indicator cache (this ADR) |
|---|---|---|
| Scope | One registry instance (one strategy's bar stream) | One batch (shared across every cycle) |
| Key | `(name, [symbol], *params, source)` — symbol inconsistently present | `(name, params, symbol, timeframe, data_fingerprint)` — symbol always present |
| Validity check | Bar fingerprint (`id`, `len`, `timestamp`, `close`) + advance classification (same-bar / expand / slide / cold) | Content-addressed key; no separate validity check needed |
| Granularity | Per-bar incremental value | Whole-series result |
| Lifetime | One strategy's execution | One batch (many strategies) |

The batch cache sits **above** `IndicatorRegistry`, not in place of it: a cycle's
design attempt would consult the batch cache first (by the composed key above) before
walking a fresh `IndicatorRegistry` over the bar stream, and — in the eventual
implementation, out of scope here — could seed a `IndicatorRegistry` walk from a batch
cache hit, or populate the batch cache from a completed `IndicatorRegistry` walk's
final series. Because the batch cache always includes `symbol` unconditionally, it
does not inherit `IndicatorRegistry`'s per-method inconsistency, and the two caches can
coexist without either needing to change its own key shape.

## Rejected alternatives

- **Key on `(indicator_name, params)` alone, without symbol/timeframe/data
  fingerprint.** Rejected: this collides different underlying data under the same key
  whenever two symbols, timeframes, or date ranges happen to request the same
  indicator params — exactly the kind of cross-contamination the epic's acceptance
  criteria call out as a correctness risk.
- **Scope the cache to the whole Strategy Lab process/run instead of one batch.**
  Rejected: a longer-lived cache risks serving values across market-data snapshots
  that were never re-validated against fresh data, breaking the same invariant
  `BacktestCache`'s per-attempt scoping exists to protect
  (`backtest_cache.py:15-18`). Batch-scoping keeps the blast radius of any keying gap
  bounded to a single batch.
- **Invent a bespoke `(start_date, end_date)` key component instead of reusing
  `compute_dataset_fingerprint`.** Rejected: a date-range pair is a coarser identity
  than the actual bar content — it cannot distinguish a data restatement or a
  backfilled gap within the same nominal range — and the codebase already has a
  well-tested, symbol-scoped content fingerprint (`market_data_cache/store.py:220`) to
  reuse instead of introducing a second, weaker notion of "same data."

## Consequences

- The sibling implementation issue can build `BatchIndicatorCache` (or an
  equivalently-named module) directly against this key composition and invalidation
  contract without re-deriving it, including the concurrency requirement stated above.
- This ADR intentionally defers: the cache's concrete data structure and storage
  mechanism, its feature flag (initially off by default, per the epic's acceptance
  criteria) and the flag's wiring into `IndicatorRegistry`/the batch Temporal workflow,
  unit/concurrency tests, and the corresponding `strategy_lab/README.md` env-var
  reference update. All of those belong to the implementation sub-issue(s), not this
  design decision.
- No existing behavior changes as a result of this ADR — it is a documentation-only
  artifact.
