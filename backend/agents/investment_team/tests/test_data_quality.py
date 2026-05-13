"""Tests for the preflight data-integrity validator (issue #375).

Mirrors the style of ``test_intraday_guard.py`` and ``test_bar_safety.py``:
small, single-assertion functions over hand-built fixtures.  No network,
no Postgres.  Pure-function checks plus a couple of integration tests
through ``MarketDataService``.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import List

import pytest

from investment_team.execution.data_quality import (
    DataIntegrityError,
    DataQualityReport,
    LiveGapMonitor,
    SymbolDataQualityReport,
    validate_market_data,
)
from investment_team.market_data_service import OHLCVBar

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _daily_bars(
    *,
    n: int = 10,
    start: str = "2024-01-02",  # Tue
    price: float = 100.0,
    volume: float = 1_000_000.0,
    skip_days: int = 0,  # advance dates by extra days every step
) -> List[OHLCVBar]:
    """Mon-Fri equity-style bars; ``start`` should be a weekday."""
    out: List[OHLCVBar] = []
    cur = datetime.fromisoformat(start)
    while len(out) < n:
        # Skip weekends.
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        out.append(
            OHLCVBar(
                date=cur.date().isoformat(),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=volume,
            )
        )
        cur += timedelta(days=1 + skip_days)
    return out


def _crypto_minute_bars(
    *,
    n: int = 10,
    start: str = "2024-05-01T12:00:00+00:00",
    price: float = 100.0,
    volume: float = 1.0,
) -> List[OHLCVBar]:
    out: List[OHLCVBar] = []
    cur = datetime.fromisoformat(start)
    for _ in range(n):
        out.append(
            OHLCVBar(
                date=cur.isoformat(),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                volume=volume,
            )
        )
        cur += timedelta(minutes=1)
    return out


# ---------------------------------------------------------------------------
# strict mode — failures
# ---------------------------------------------------------------------------


def test_strict_raises_on_gap() -> None:
    bars = _daily_bars(n=20)
    # Drop a non-weekend, non-holiday day so the calendar flags it.
    bars.pop(5)  # the 6th business day in the run
    bars.pop(5)
    bars.pop(5)
    bars.pop(5)
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": bars},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    report = excinfo.value.report
    assert report.severity == "fail"
    assert report.per_symbol["AAPL"].gaps >= 4


def test_strict_raises_on_ohlc_violation() -> None:
    bars = _daily_bars(n=10)
    bars[3] = bars[3].model_copy(update={"high": bars[3].open - 1.0})
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": bars},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    assert excinfo.value.report.per_symbol["AAPL"].ohlc_violations == 1


def test_strict_raises_on_nan_price() -> None:
    bars = _daily_bars(n=10)
    bars[2] = bars[2].model_copy(update={"close": float("nan")})
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": bars},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    assert excinfo.value.report.per_symbol["AAPL"].nan_or_negative_prices == 1


def test_strict_raises_on_negative_price() -> None:
    bars = _daily_bars(n=10)
    bars[4] = bars[4].model_copy(update={"low": -5.0})
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": bars},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    assert excinfo.value.report.per_symbol["AAPL"].nan_or_negative_prices == 1


def test_strict_raises_on_duplicate_timestamp() -> None:
    bars = _daily_bars(n=10)
    # Make bars[5] a duplicate of bars[4] without changing other invariants.
    bars[5] = bars[5].model_copy(update={"date": bars[4].date})
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": bars},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    assert excinfo.value.report.per_symbol["AAPL"].duplicate_timestamps >= 1


def test_strict_raises_on_frequency_mismatch() -> None:
    bars = _crypto_minute_bars(n=20)
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"BTC": bars},
            expected_frequency="1d",  # claim daily on minute data
            asset_class="crypto",
            mode="strict",
        )
    rep = excinfo.value.report.per_symbol["BTC"]
    assert rep.inferred_frequency == "1m"
    assert rep.expected_frequency == "1d"


def test_strict_raises_on_empty_input() -> None:
    with pytest.raises(DataIntegrityError):
        validate_market_data(
            bars_by_symbol={},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )


# ---------------------------------------------------------------------------
# warn mode
# ---------------------------------------------------------------------------


def test_warn_mode_returns_report_no_raise() -> None:
    bars = _daily_bars(n=10)
    bars[3] = bars[3].model_copy(update={"high": bars[3].open - 1.0})
    # ``warn`` mode never raises even with a fatal-class issue.
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="warn",
    )
    assert isinstance(report, DataQualityReport)
    assert report.severity == "fail"
    assert report.per_symbol["AAPL"].ohlc_violations == 1


def test_zero_volume_warns_not_fails() -> None:
    bars = _daily_bars(n=10)
    bars[5] = bars[5].model_copy(update={"volume": 0.0})
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    assert report.severity == "warn"
    assert report.per_symbol["AAPL"].zero_volume_bars == 1


def test_volume_outlier_detected() -> None:
    # With a single extreme outlier among N near-constant values the max
    # z-score is roughly sqrt(N).  The default z_threshold=6 therefore
    # only fires on ~36+ bars; we tighten the threshold here to keep the
    # fixture small while still exercising the rule.
    bars = _daily_bars(n=20, volume=1.0)
    bars[10] = bars[10].model_copy(update={"volume": 1_000_000.0})
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
        z_threshold=3.0,
    )
    assert report.severity == "warn"
    assert report.per_symbol["AAPL"].volume_outliers >= 1


def test_warn_mode_returns_ok_on_clean_series() -> None:
    bars = _daily_bars(n=20)
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    assert report.severity == "ok"
    assert report.per_symbol["AAPL"].issues == []


# ---------------------------------------------------------------------------
# Cross-symbol alignment
# ---------------------------------------------------------------------------


def test_cross_symbol_alignment_miss() -> None:
    aapl = _daily_bars(n=20, start="2024-01-02")
    msft = _daily_bars(n=20, start="2024-01-09")  # offset by a week
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": aapl, "MSFT": msft},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    assert excinfo.value.report.cross_symbol_alignment_misses >= 1


def test_aligned_multi_symbol_ok() -> None:
    aapl = _daily_bars(n=20)
    msft = _daily_bars(n=20)
    report = validate_market_data(
        bars_by_symbol={"AAPL": aapl, "MSFT": msft},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    assert report.severity == "ok"
    assert report.cross_symbol_alignment_misses == 0


# ---------------------------------------------------------------------------
# Calendar handling
# ---------------------------------------------------------------------------


def test_equity_weekend_not_flagged_as_gap() -> None:
    # A clean Mon-Fri series should produce zero gaps on the equity calendar.
    bars = _daily_bars(n=10, start="2024-01-02")  # Tue Jan 2
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    assert report.per_symbol["AAPL"].gaps == 0


def test_crypto_no_weekend_gap() -> None:
    # 14 consecutive calendar days (incl. weekends) at 1d frequency.
    bars: List[OHLCVBar] = []
    cur = datetime.fromisoformat("2024-05-01")
    for _ in range(14):
        bars.append(
            OHLCVBar(
                date=cur.date().isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=10.0,
            )
        )
        cur += timedelta(days=1)
    report = validate_market_data(
        bars_by_symbol={"BTC": bars},
        expected_frequency="1d",
        asset_class="crypto",
        mode="strict",
    )
    assert report.severity == "ok"
    assert report.per_symbol["BTC"].gaps == 0


# ---------------------------------------------------------------------------
# Serialization round-trip (BacktestResult.data_quality_report stores dict)
# ---------------------------------------------------------------------------


def test_report_serialization_roundtrip() -> None:
    bars = _daily_bars(n=10)
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    payload = report.model_dump()
    assert isinstance(payload, dict)
    assert payload["severity"] == "ok"
    # Round-trip back to a typed model so we know the dict shape is canonical.
    reconstructed = DataQualityReport.model_validate(payload)
    assert reconstructed.severity == report.severity
    assert "AAPL" in reconstructed.per_symbol
    assert isinstance(reconstructed.per_symbol["AAPL"], SymbolDataQualityReport)


# ---------------------------------------------------------------------------
# LiveGapMonitor
# ---------------------------------------------------------------------------


def test_live_gap_monitor_emits_warning_on_large_gap() -> None:
    monitor = LiveGapMonitor(bar_frequency="1m", threshold_multiple=5.0)
    # First bar primes state, no warning.
    assert monitor.observe("BTC", "2024-05-01T12:00:00+00:00") is None
    # 1-minute later → no warning (within 5x).
    assert monitor.observe("BTC", "2024-05-01T12:01:00+00:00") is None
    # 10-minute later → 10x → warning.
    assert monitor.observe("BTC", "2024-05-01T12:11:00+00:00") == "data_quality:live_gap:BTC"


def test_live_gap_monitor_per_symbol_isolation() -> None:
    monitor = LiveGapMonitor(bar_frequency="1m", threshold_multiple=5.0)
    monitor.observe("BTC", "2024-05-01T12:00:00+00:00")
    monitor.observe("ETH", "2024-05-01T12:00:00+00:00")
    # BTC gap big, ETH gap small.
    assert monitor.observe("BTC", "2024-05-01T12:30:00+00:00") == "data_quality:live_gap:BTC"
    assert monitor.observe("ETH", "2024-05-01T12:01:00+00:00") is None


def test_live_gap_monitor_unknown_frequency_is_silent() -> None:
    monitor = LiveGapMonitor(bar_frequency="bogus")
    assert monitor.observe("BTC", "2024-05-01T12:00:00+00:00") is None
    # Even a huge gap doesn't fire when frequency is unrecognised.
    assert monitor.observe("BTC", "2025-01-01T00:00:00+00:00") is None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_single_bar_does_not_fail_on_frequency() -> None:
    # One-bar warm-up window: cannot infer frequency, must not fail.
    bars = _daily_bars(n=1)
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    assert report.severity == "ok"
    assert report.per_symbol["AAPL"].inferred_frequency == "unknown"


def test_inf_price_caught_as_nan() -> None:
    bars = _daily_bars(n=10)
    bars[3] = bars[3].model_copy(update={"open": math.inf})
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": bars},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    assert excinfo.value.report.per_symbol["AAPL"].nan_or_negative_prices == 1


def test_unknown_asset_class_falls_back_to_continuous_calendar() -> None:
    # 14 consecutive days; on an unknown asset class we treat as continuous.
    bars: List[OHLCVBar] = []
    cur = datetime.fromisoformat("2024-05-01")
    for _ in range(14):
        bars.append(
            OHLCVBar(
                date=cur.date().isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=10.0,
            )
        )
        cur += timedelta(days=1)
    report = validate_market_data(
        bars_by_symbol={"X": bars},
        expected_frequency="1d",
        asset_class="exotic",
        mode="strict",
    )
    assert report.severity == "ok"


def test_holiday_window_unsupported_skips_gap_detection() -> None:
    """Equity series outside [2018, 2030] degrades to 'skip gaps + note'.

    Reviewer P1 (codex): without this guard, true US-equity holidays in
    e.g. a 2017 backtest would all be flagged as missing bars and the
    severity would flip to ``fail``.  We surface a structured
    ``calendar_window_unsupported`` issue instead so callers can opt in
    to richer calendars later.
    """
    # Build a clean 2017 weekday-only series.  Jan 16 2017 was MLK Day —
    # if our holiday set were applied, it would be flagged as a gap.
    bars: List[OHLCVBar] = []
    cur = datetime.fromisoformat("2017-01-03")  # Tue
    for _ in range(30):
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        # Skip MLK 2017 (Jan 16) to mimic real US-equity data.
        if cur.date().isoformat() == "2017-01-16":
            cur += timedelta(days=1)
            continue
        bars.append(
            OHLCVBar(
                date=cur.date().isoformat(),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1_000_000.0,
            )
        )
        cur += timedelta(days=1)
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    sym_report = report.per_symbol["AAPL"]
    assert sym_report.gaps == 0
    assert "calendar_window_unsupported" in sym_report.issues
    # Severity stays "ok" (or "warn" only via other rules) — must NOT fail
    # purely because the holiday window doesn't cover 2017.
    assert report.severity != "fail"


def test_holiday_window_supported_in_range() -> None:
    """Sanity: a 2024 series still applies the holiday filter."""
    # Run jumps across MLK 2024 (Jan 15) — actual data correctly omits it.
    bars: List[OHLCVBar] = []
    cur = datetime.fromisoformat("2024-01-09")  # Tue
    target_dates: List[str] = []
    while len(target_dates) < 10:
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        target_dates.append(cur.date().isoformat())
        cur += timedelta(days=1)
    # Drop MLK from the data; without the holiday filter this would count
    # as a gap, but with the filter it should be expected-as-closed.
    target_dates = [d for d in target_dates if d != "2024-01-15"]
    bars = [
        OHLCVBar(date=d, open=100.0, high=101.0, low=99.0, close=100.0, volume=1.0)
        for d in target_dates
    ]
    report = validate_market_data(
        bars_by_symbol={"AAPL": bars},
        expected_frequency="1d",
        asset_class="stocks",
        mode="strict",
    )
    assert report.severity == "ok"
    assert report.per_symbol["AAPL"].gaps == 0


def test_live_gap_monitor_ignores_out_of_order_bars() -> None:
    """Reviewer P2 (codex): a stale bar must not poison the next in-order bar.

    Concretely: at 1m frequency, two in-order bars 1 minute apart should
    NOT trigger a gap warning even if a much-later out-of-order bar
    arrived in between (which we then expect the monitor to drop without
    mutating its internal last-seen timestamp).
    """
    monitor = LiveGapMonitor(bar_frequency="1m", threshold_multiple=5.0)
    monitor.observe("BTC", "2024-05-01T12:00:00+00:00")  # prime
    monitor.observe("BTC", "2024-05-01T12:01:00+00:00")  # in-order, no warning
    # A late/out-of-order bar arrives.  Pre-fix this would overwrite
    # ``_last_ts`` with this stale ts, then the next 12:02 bar would be
    # compared against e.g. 11:30 → 32m delta → false gap warning.
    assert monitor.observe("BTC", "2024-05-01T11:30:00+00:00") is None
    # The next real-time bar is just 1 minute after 12:01 — still in order
    # and within threshold.  Must NOT fire.
    assert monitor.observe("BTC", "2024-05-01T12:02:00+00:00") is None


def test_live_gap_monitor_advances_state_after_real_gap() -> None:
    """Two consecutive over-threshold gaps each fire once (no chaining)."""
    monitor = LiveGapMonitor(bar_frequency="1m", threshold_multiple=5.0)
    monitor.observe("BTC", "2024-05-01T12:00:00+00:00")
    # 30-minute gap ⇒ warning, state advances to 12:30.
    assert monitor.observe("BTC", "2024-05-01T12:30:00+00:00") == "data_quality:live_gap:BTC"
    # 1 minute later ⇒ in-order, no warning.
    assert monitor.observe("BTC", "2024-05-01T12:31:00+00:00") is None


def test_intraday_bars_clean_series() -> None:
    bars = _crypto_minute_bars(n=60)
    report = validate_market_data(
        bars_by_symbol={"BTC": bars},
        expected_frequency="1m",
        asset_class="crypto",
        mode="strict",
    )
    assert report.severity == "ok"
    assert report.per_symbol["BTC"].inferred_frequency == "1m"


# ---------------------------------------------------------------------------
# Forex / vendor-rounded data — issues observed in production strategy lab
# ---------------------------------------------------------------------------


def test_forex_zero_volume_not_flagged() -> None:
    """FX bars structurally have zero volume (decentralized OTC).

    market_data_service emits ``volume=0`` for every Alpha Vantage FX bar
    and Yahoo Finance returns 0 for ``=X`` pairs. Flagging this as a
    quality issue produces noise on every forex backtest. Volume rules
    must skip the forex asset class entirely.
    """
    bars: List[OHLCVBar] = []
    cur = datetime.fromisoformat("2024-01-02")  # Tue
    while len(bars) < 20:
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        bars.append(
            OHLCVBar(
                date=cur.date().isoformat(),
                open=1.0512,
                high=1.0514,
                low=1.0510,
                close=1.0513,
                volume=0.0,
            )
        )
        cur += timedelta(days=1)
    report = validate_market_data(
        bars_by_symbol={"EURUSD=X": bars},
        expected_frequency="1d",
        asset_class="forex",
        mode="strict",
    )
    assert report.severity == "ok"
    assert report.per_symbol["EURUSD=X"].zero_volume_bars == 0
    assert "zero_volume_bars" not in report.per_symbol["EURUSD=X"].issues


def test_ohlc_tolerance_absorbs_forex_vendor_noise_eurusd() -> None:
    """4-decimal vendor rounding can flip H/C by up to ~1e-4 absolute.

    Reviewer (production): the Strategy Lab forex run failed with
    ``ohlc_violations`` because the legacy ``eps = 1e-9 * scale``
    tolerance was orders of magnitude tighter than the rounding noise
    floor (~1e-4) introduced by market_data_service when it rounds
    Yahoo Finance bars to 4 decimals. A bar like H=1.0512 with C=1.0513
    must pass — the underlying market data is internally consistent;
    only independent rounding made high a tick below close.
    """
    bars = [
        OHLCVBar(
            date="2024-01-02",
            open=1.0512,
            high=1.0512,   # one tick below close, but within rounding-noise floor
            low=1.0510,
            close=1.0513,
            volume=0.0,
        ),
        OHLCVBar(
            date="2024-01-03",
            open=1.0513,
            high=1.0515,
            low=1.0511,
            close=1.0514,
            volume=0.0,
        ),
    ]
    report = validate_market_data(
        bars_by_symbol={"EURUSD=X": bars},
        expected_frequency="1d",
        asset_class="forex",
        mode="strict",
    )
    assert report.per_symbol["EURUSD=X"].ohlc_violations == 0


def test_ohlc_tolerance_absorbs_forex_vendor_noise_usdjpy() -> None:
    """USD/JPY trades ~150; pip = 1e-2. Yahoo daily bars can drift several
    pips between snapshots even when the underlying data is internally
    consistent. The forex tolerance must scale with price level so a
    USDJPY high a few pips below the close (e.g. 150.10 vs 150.12)
    doesn't false-fail the run.
    """
    bars = [
        OHLCVBar(
            date="2024-01-02",
            open=150.1100,
            high=150.1000,   # 0.01 = 1 pip below close — pure vendor noise
            low=150.0500,
            close=150.1200,
            volume=0.0,
        ),
        OHLCVBar(
            date="2024-01-03",
            open=150.1300,
            high=150.1500,
            low=150.1000,
            close=150.1400,
            volume=0.0,
        ),
    ]
    report = validate_market_data(
        bars_by_symbol={"USDJPY=X": bars},
        expected_frequency="1d",
        asset_class="forex",
        mode="strict",
    )
    assert report.per_symbol["USDJPY=X"].ohlc_violations == 0


def test_df_to_bars_repairs_ohlc_invariants() -> None:
    """Yahoo Finance daily FX bars sometimes report H < max(O, C) or L >
    min(O, C). market_data_service repairs the invariants at fetch time
    so downstream consumers (strategies, validator, indicators) always
    see coherent bars.
    """
    pd = pytest.importorskip("pandas")
    from investment_team.market_data_service import MarketDataService

    # Row 1: H quoted a tick below close — repair lifts H to close.
    # Row 2: L quoted a tick above open — repair lowers L to open.
    # Row 3: already invariant-clean — no repair.
    df = pd.DataFrame(
        [
            {"Open": 1.0512, "High": 1.0512, "Low": 1.0509, "Close": 1.0513, "Volume": 0},
            {"Open": 150.1100, "High": 150.1500, "Low": 150.1200, "Close": 150.1400, "Volume": 0},
            {"Open": 1.0513, "High": 1.0515, "Low": 1.0511, "Close": 1.0514, "Volume": 0},
        ],
        index=pd.to_datetime(["2024-01-02", "2024-01-03", "2024-01-04"]),
    )
    bars = MarketDataService._df_to_bars(df)
    assert len(bars) == 3
    # Row 1 was repaired: H lifted from 1.0512 to 1.0513 (close).
    assert bars[0].high == 1.0513
    assert bars[0].low == 1.0509
    # Row 2 was repaired: L lowered from 150.12 to 150.11 (open).
    assert bars[1].high == 150.1500
    assert bars[1].low == 150.1100
    # Row 3 untouched.
    assert bars[2].high == 1.0515
    assert bars[2].low == 1.0511
    # Resulting bars must all satisfy OHLC invariants.
    for b in bars:
        assert b.high >= max(b.open, b.low, b.close)
        assert b.low <= min(b.open, b.high, b.close)


def test_cache_table_to_bars_repairs_ohlc_invariants() -> None:
    """Parquet snapshots persisted before the fetch-time repair landed
    still carry broken OHLC invariants. ``_table_to_bars`` self-heals on
    read so the cache doesn't have to be wiped to recover.
    """
    pa = pytest.importorskip("pyarrow")
    from investment_team.market_data_cache.store import _get_parquet_schema, _table_to_bars

    table = pa.Table.from_pydict(
        {
            "date": ["2024-01-02", "2024-01-03"],
            # Row 0: H below close — pre-fix corruption that lived in cache.
            # Row 1: clean — no repair.
            "open": [1.0512, 1.0513],
            "high": [1.0512, 1.0515],
            "low": [1.0509, 1.0511],
            "close": [1.0513, 1.0514],
            "volume": [0.0, 0.0],
        },
        schema=_get_parquet_schema(),
    )
    bars = _table_to_bars(table)
    assert len(bars) == 2
    # Row 0 was repaired: H lifted to close.
    assert bars[0].high == 1.0513
    # Row 1 untouched.
    assert bars[1].high == 1.0515
    for b in bars:
        assert b.high >= max(b.open, b.low, b.close)
        assert b.low <= min(b.open, b.high, b.close)


def test_forex_materially_broken_bar_still_flags() -> None:
    """The more generous forex tolerance must not mask real corruption.

    A high quoted ~1% below the close on EUR/USD (e.g. 1.04 vs 1.05) is
    clearly broken — well outside any reasonable vendor-noise floor —
    and must still raise.
    """
    bars: List[OHLCVBar] = []
    cur = datetime.fromisoformat("2024-01-02")
    while len(bars) < 10:
        while cur.weekday() >= 5:
            cur += timedelta(days=1)
        bars.append(
            OHLCVBar(
                date=cur.date().isoformat(),
                open=1.0512,
                high=1.0514,
                low=1.0510,
                close=1.0513,
                volume=0.0,
            )
        )
        cur += timedelta(days=1)
    # Corrupt one bar with a high 1% below close — way above the ~10-pip
    # noise tolerance.
    bars[3] = bars[3].model_copy(update={"high": 1.0400})
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"EURUSD=X": bars},
            expected_frequency="1d",
            asset_class="forex",
            mode="strict",
        )
    assert excinfo.value.report.per_symbol["EURUSD=X"].ohlc_violations == 1


def test_ohlc_tolerance_still_catches_material_violation_at_stock_prices() -> None:
    """Loosening the OHLC tolerance must not mask real corruption.

    A 1-cent OHLC violation at a $100 stock (1e-4 relative) is the
    borderline; anything materially larger must still flag. Use a $1
    violation to confirm — the legacy test exercised the same
    construction so this lives alongside as the regression guard for the
    new tolerance.
    """
    bars = _daily_bars(n=10)
    bars[3] = bars[3].model_copy(update={"high": bars[3].open - 1.0})
    with pytest.raises(DataIntegrityError) as excinfo:
        validate_market_data(
            bars_by_symbol={"AAPL": bars},
            expected_frequency="1d",
            asset_class="stocks",
            mode="strict",
        )
    assert excinfo.value.report.per_symbol["AAPL"].ohlc_violations == 1
