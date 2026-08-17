"""Tests for ``IndicatorRegistry``'s optional ``BatchIndicatorCache`` wiring.

``IndicatorRegistry`` (``strategy_lab/indicators/streaming.py``) can
optionally be given a ``BatchIndicatorCache`` reference at construction, but
only actually consults it via ``resolve_indicator`` when the
``STRATEGY_LAB_BATCH_INDICATOR_CACHE_ENABLED`` env var is truthy (default:
on). These tests cover both flag states plus the guard conditions
(``timeframe``/``symbol`` must be present) that gate consultation even when
the flag is on, and confirm the cached and uncached code paths always agree
on the value returned — the wiring may change *how* a value is produced, but
never *what* value comes back.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import List

from investment_team.strategy_lab.batch_indicator_cache import BatchIndicatorCache
from investment_team.strategy_lab.indicators.streaming import (
    IndicatorRegistry,
    resolve_indicator,
)

_ENV_VAR = "STRATEGY_LAB_BATCH_INDICATOR_CACHE_ENABLED"


@dataclass
class _SymBar:
    """Bar-shaped record carrying both ``symbol`` and ``date``.

    ``date`` is required for ``BatchIndicatorCache`` to fingerprint the bar
    content (OHLCV fields hashed in supplied order). Real ``contract.Bar``
    streaming bars carry ``timestamp``, not ``date`` — :class:`_ContractLikeBar`
    below models that shape instead.
    """

    symbol: str
    timestamp: str
    date: str
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 1.0


@dataclass
class _Bar:
    """Bar-shaped record with no ``symbol`` attribute at all."""

    timestamp: str
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 1.0


@dataclass
class _ContractLikeBar:
    """Bar-shaped record matching ``trading_service.strategy.contract.Bar``:
    carries ``symbol`` and ``timestamp`` but — unlike ``_SymBar`` above — no
    ``date`` attribute. This is the actual bar shape the streaming engine
    feeds ``IndicatorRegistry`` in production."""

    symbol: str
    timestamp: str
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 1.0


def _series(symbol: str, n: int, seed: int = 0) -> List[_SymBar]:
    rng = random.Random(seed)
    bars: List[_SymBar] = []
    for i in range(n):
        close = 100.0 + rng.uniform(-3.0, 3.0) + i * 0.3
        day = f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
        bars.append(
            _SymBar(
                symbol=symbol,
                timestamp=day,
                date=day,
                open=close - 0.1,
                high=close + 0.4,
                low=close - 0.4,
                close=close,
                volume=1_000.0 + i,
            )
        )
    return bars


def _symbolless_series(n: int, seed: int = 0) -> List[_Bar]:
    rng = random.Random(seed)
    bars: List[_Bar] = []
    for i in range(n):
        close = 100.0 + rng.uniform(-3.0, 3.0) + i * 0.3
        bars.append(
            _Bar(
                timestamp=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=close - 0.1,
                high=close + 0.4,
                low=close - 0.4,
                close=close,
                volume=1_000.0 + i,
            )
        )
    return bars


def _contract_like_series(symbol: str, n: int, seed: int = 0) -> List[_ContractLikeBar]:
    rng = random.Random(seed)
    bars: List[_ContractLikeBar] = []
    for i in range(n):
        close = 100.0 + rng.uniform(-3.0, 3.0) + i * 0.3
        bars.append(
            _ContractLikeBar(
                symbol=symbol,
                timestamp=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=close - 0.1,
                high=close + 0.4,
                low=close - 0.4,
                close=close,
                volume=1_000.0 + i,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Constructor contract
# ---------------------------------------------------------------------------


def test_default_construction_has_no_batch_cache() -> None:
    """A plain ``IndicatorRegistry()`` (every existing call site) never has
    a batch cache, regardless of the env var — matches today's behavior."""
    reg = IndicatorRegistry()
    assert reg._batch_cache is None
    assert reg._timeframe == ""


def test_flag_off_discards_passed_cache_at_construction(monkeypatch) -> None:
    """With the flag set falsy, a ``batch_cache`` argument is
    discarded immediately at construction, not merely ignored later."""
    monkeypatch.setenv(_ENV_VAR, "false")
    cache = BatchIndicatorCache()
    reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    assert reg._batch_cache is None
    assert reg._timeframe == "1d"


def test_flag_on_keeps_passed_cache(monkeypatch) -> None:
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    assert reg._batch_cache is cache


def test_default_flag_unset_keeps_passed_cache(monkeypatch) -> None:
    """The flag now defaults on: with the env var unset, a passed
    ``batch_cache`` is kept (the flipped bake-in default)."""
    monkeypatch.delenv(_ENV_VAR, raising=False)
    cache = BatchIndicatorCache()
    reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    assert reg._batch_cache is cache


# ---------------------------------------------------------------------------
# Flag off (explicit opt-out): behavior byte-for-byte identical, cache never touched
# ---------------------------------------------------------------------------


def test_flag_off_never_consults_cache_and_matches_uncached_values(monkeypatch) -> None:
    monkeypatch.setenv(_ENV_VAR, "false")
    cache = BatchIndicatorCache()
    bars = _series("AAPL", 60)
    reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    baseline = IndicatorRegistry()

    for n in range(26, 61):
        sub = bars[:n]
        for name, params in (
            ("sma", {"period": 10}),
            ("rsi", {"period": 14}),
            ("macd", {"fast": 12, "slow": 26, "signal": 9, "output": "signal"}),
        ):
            got = resolve_indicator(reg, name, sub, **params)
            want = resolve_indicator(baseline, name, sub, **params)
            assert got == want, f"n={n} name={name} diverged: {got!r} != {want!r}"

    assert cache.hits == 0
    assert cache.misses == 0


# ---------------------------------------------------------------------------
# Flag on, but a required guard condition is missing: still no consultation
# ---------------------------------------------------------------------------


def test_flag_on_without_timeframe_never_consults_cache(monkeypatch) -> None:
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    bars = _series("AAPL", 40)
    reg = IndicatorRegistry(batch_cache=cache)  # timeframe left at default ""
    baseline = IndicatorRegistry()

    got = resolve_indicator(reg, "sma", bars, period=10)
    want = resolve_indicator(baseline, "sma", bars, period=10)
    assert got == want
    assert cache.hits == 0
    assert cache.misses == 0


def test_flag_on_without_symbol_on_bars_never_consults_cache(monkeypatch) -> None:
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    bars = _symbolless_series(40)
    reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    baseline = IndicatorRegistry()

    got = resolve_indicator(reg, "sma", bars, period=10)
    want = resolve_indicator(baseline, "sma", bars, period=10)
    assert got == want
    assert cache.hits == 0
    assert cache.misses == 0


def test_flag_on_without_date_on_bars_never_consults_cache(monkeypatch) -> None:
    """``contract.Bar``-shaped bars (``symbol``/``timestamp``, no ``date``) —
    the real streaming-engine bar shape — must not crash
    ``BatchIndicatorCache._data_fingerprint`` (which hashes on ``bar.date``).
    Consultation is skipped for this bar shape rather than raising."""
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    bars = _contract_like_series("AAPL", 40)
    reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    baseline = IndicatorRegistry()

    got = resolve_indicator(reg, "sma", bars, period=10)
    want = resolve_indicator(baseline, "sma", bars, period=10)
    assert got == want
    assert cache.hits == 0
    assert cache.misses == 0


# ---------------------------------------------------------------------------
# Flag on, symbol + timeframe present: real cross-registry consultation
# ---------------------------------------------------------------------------


def test_flag_on_second_registry_hits_cache_with_matching_value(monkeypatch) -> None:
    """Two independent registries (simulating two strategies in one batch)
    sharing one cache, same symbol/timeframe/bars: the second call for the
    same indicator+params is a cache hit and returns the same value the
    first (uncached) call computed."""
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    bars = _series("AAPL", 60)

    reg_a = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    reg_b = IndicatorRegistry(batch_cache=cache, timeframe="1d")

    v_a = resolve_indicator(reg_a, "macd", bars, fast=12, slow=26, signal=9, output="signal")
    assert cache.misses == 1
    assert cache.hits == 0

    v_b = resolve_indicator(reg_b, "macd", bars, fast=12, slow=26, signal=9, output="signal")
    assert cache.misses == 1
    assert cache.hits == 1
    assert v_b == v_a

    # Parity against a registry that never touches the cache at all.
    baseline = IndicatorRegistry()
    v_ref = resolve_indicator(baseline, "macd", bars, fast=12, slow=26, signal=9, output="signal")
    assert v_a == v_ref


def test_flag_on_distinct_symbols_produce_distinct_cache_entries(monkeypatch) -> None:
    """Two registries computing the same indicator/params but over different
    symbols must land in different cache slots (the ADR's invalidation
    contract) rather than colliding or cross-contaminating."""
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    aapl = _series("AAPL", 60, seed=1)
    msft = _series("MSFT", 60, seed=2)

    reg_a = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    reg_b = IndicatorRegistry(batch_cache=cache, timeframe="1d")

    v_a = resolve_indicator(reg_a, "sma", aapl, period=10)
    v_b = resolve_indicator(reg_b, "sma", msft, period=10)

    assert cache.misses == 2
    assert cache.hits == 0

    ref_a = resolve_indicator(IndicatorRegistry(), "sma", aapl, period=10)
    ref_b = resolve_indicator(IndicatorRegistry(), "sma", msft, period=10)
    assert v_a == ref_a
    assert v_b == ref_b


def test_flag_on_distinct_params_produce_distinct_cache_entries(monkeypatch) -> None:
    """Same symbol/timeframe/bars, different indicator params: must not
    collide on one cache slot."""
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    bars = _series("AAPL", 60)

    reg_a = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    reg_b = IndicatorRegistry(batch_cache=cache, timeframe="1d")

    v_10 = resolve_indicator(reg_a, "sma", bars, period=10)
    v_20 = resolve_indicator(reg_b, "sma", bars, period=20)

    assert cache.misses == 2
    assert cache.hits == 0
    assert v_10 != v_20


def test_cache_hit_clears_stale_streaming_state_before_next_expand(monkeypatch) -> None:
    """A cache hit must not leave ``reg._state`` keyed to a different prefix.

    Fingerprints only identify the trailing bar (id/len/timestamp/close). If
    a registry walked history H1, then returned a cache hit for H2 with the
    same length and last bar but a revised earlier prefix, the next one-bar
    extension of H2 would otherwise classify as ``expand`` and MACD would
    step from H1's streaming state instead of H2.
    """
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()
    h1 = _series("AAPL", 50, seed=1)
    h2 = list(h1)
    early = h1[0]
    h2[0] = _SymBar(
        symbol=early.symbol,
        timestamp=early.timestamp,
        date=early.date,
        open=early.open + 25.0,
        high=early.high + 25.0,
        low=early.low + 25.0,
        close=early.close + 25.0,
        volume=early.volume,
    )
    extra = _SymBar(
        symbol="AAPL",
        timestamp="2024-02-23",
        date="2024-02-23",
        open=110.0,
        high=111.0,
        low=109.0,
        close=110.5,
        volume=1_000.0,
    )
    h2_plus = h2 + [extra]
    macd_params = {"fast": 12, "slow": 26, "signal": 9, "output": "signal"}

    prefill = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    resolve_indicator(prefill, "macd", h2, **macd_params)
    assert cache.misses == 1
    assert cache.hits == 0

    reg = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    resolve_indicator(reg, "macd", h1, **macd_params)
    assert cache.misses == 2
    assert reg._state

    hit_value = resolve_indicator(reg, "macd", h2, **macd_params)
    assert cache.hits == 1
    assert hit_value == resolve_indicator(IndicatorRegistry(), "macd", h2, **macd_params)
    assert reg._state == {}

    got = resolve_indicator(reg, "macd", h2_plus, **macd_params)
    want = resolve_indicator(IndicatorRegistry(), "macd", h2_plus, **macd_params)
    assert got == want


def test_flag_on_reversed_bar_order_does_not_reuse_cached_sma(monkeypatch) -> None:
    """Same dated bars in a different sequence must not share a cache entry.

    SMA's trailing window is order-sensitive: closes ``[1, 2, 10]`` with
    period 2 average to ``6.0`` while ``[10, 2, 1]`` average to ``1.5``. A
    date-sorted fingerprint would return the first call's value for both.
    """
    monkeypatch.setenv(_ENV_VAR, "true")
    cache = BatchIndicatorCache()

    def _bar(date: str, close: float) -> _SymBar:
        return _SymBar(
            symbol="AAPL",
            timestamp=date,
            date=date,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1.0,
        )

    chronological = [_bar("2023-01-02", 1.0), _bar("2023-01-03", 2.0), _bar("2023-01-04", 10.0)]
    reversed_order = list(reversed(chronological))

    reg_a = IndicatorRegistry(batch_cache=cache, timeframe="1d")
    reg_b = IndicatorRegistry(batch_cache=cache, timeframe="1d")

    v_chrono = resolve_indicator(reg_a, "sma", chronological, period=2)
    v_rev = resolve_indicator(reg_b, "sma", reversed_order, period=2)

    assert cache.misses == 2
    assert cache.hits == 0
    assert v_chrono == 6.0
    assert v_rev == 1.5
    assert v_chrono == resolve_indicator(IndicatorRegistry(), "sma", chronological, period=2)
    assert v_rev == resolve_indicator(IndicatorRegistry(), "sma", reversed_order, period=2)
