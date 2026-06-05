"""Coverage for ``investment_team.market_data_service``.

Targets:
* ``_max_universe_symbols`` env-var parsing.
* ``compute_adv_from_bars`` corner cases.
* ``_fetch_with_providers`` chain behaviour (success, all-failed,
  exception swallowed).
* ``_fetch_twelve_data`` / ``_fetch_coingecko`` / ``_fetch_alphavantage``
  with patched ``httpx.Client``.
* ``_fetch_yahoo`` with a stubbed ``yfinance`` module.
* ``_df_to_bars`` invariant normalisation.
* ``_warn_on_asset_class_mismatch`` warning behaviour.

Every provider HTTP call is faked at the ``httpx.Client`` boundary so
no network traffic fires.
"""

from __future__ import annotations

import math
import sys
import types
from typing import Any, Dict, List, Optional, Tuple

import pytest

from investment_team.market_data_service import (
    MarketDataService,
    OHLCVBar,
    _max_universe_symbols,
    compute_adv_from_bars,
    trailing_real_bars,
)

# ---------------------------------------------------------------------------
# _max_universe_symbols
# ---------------------------------------------------------------------------


def test_max_universe_symbols_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS", raising=False)
    assert _max_universe_symbols() == 20


def test_max_universe_symbols_falls_back_on_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS", "abc")
    assert _max_universe_symbols() == 20


def test_max_universe_symbols_clamps_below_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS", "0")
    assert _max_universe_symbols() == 1


def test_max_universe_symbols_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS", "7")
    assert _max_universe_symbols() == 7


# ---------------------------------------------------------------------------
# compute_adv_from_bars
# ---------------------------------------------------------------------------


def _bar(close: float = 100.0, volume: float = 1_000_000.0) -> OHLCVBar:
    return OHLCVBar(
        date="2024-01-01", open=close, high=close + 0.5, low=close - 0.5, close=close, volume=volume
    )


def test_compute_adv_returns_none_when_empty() -> None:
    assert compute_adv_from_bars([]) is None


def test_compute_adv_returns_none_when_lookback_zero() -> None:
    assert compute_adv_from_bars([_bar()] * 25, lookback=0) is None


def test_compute_adv_returns_none_when_window_short() -> None:
    bars = [_bar()] * 5
    assert compute_adv_from_bars(bars, lookback=20) is None


def test_compute_adv_returns_none_when_all_volume_zero() -> None:
    bars = [_bar(volume=0)] * 20
    assert compute_adv_from_bars(bars, lookback=20) is None


def test_compute_adv_averages_close_times_volume() -> None:
    bars = [_bar(close=100.0, volume=1_000_000.0)] * 20
    val = compute_adv_from_bars(bars, lookback=20)
    assert val == 100_000_000.0


def test_compute_adv_unaffected_by_coerced_volume() -> None:
    """Bars whose bad volume was coerced to 0.0 never poison ADV with NaN.

    A window mixing real-volume bars with coerced-to-zero bars yields the mean
    over the positive-volume bars (zeros are excluded), never ``NaN``.
    """
    bars = [_bar(close=100.0, volume=1_000_000.0)] * 18 + [_bar(close=100.0, volume=0.0)] * 2
    val = compute_adv_from_bars(bars, lookback=20)
    assert val == 100_000_000.0
    assert val is not None and math.isfinite(val)
    # Every bar zero-volume → None (documented), still never NaN.
    assert compute_adv_from_bars([_bar(volume=0.0)] * 20, lookback=20) is None


def test_compute_adv_excludes_nonfinite_volume() -> None:
    """A non-finite volume is treated as zero (excluded), so the ADV value
    matches the coerced derived-ADV cache key and a cached replay.

    Without excluding ``+inf`` (``inf > 0`` passes the raw filter) the compute
    path would return ``inf`` while the cache key hashed it as ``0.0`` — storing
    ``inf`` under the zero-volume fingerprint and serving it to a later genuine
    zero-volume window.
    """
    # All-inf and all-NaN windows behave like all-zero: no real volume → None.
    assert compute_adv_from_bars([_bar(volume=float("inf"))] * 20, lookback=20) is None
    assert compute_adv_from_bars([_bar(volume=float("nan"))] * 20, lookback=20) is None
    # A +inf bar mixed with finite-volume bars is excluded, never poisoning the
    # mean with inf; ADV is the mean over the finite bars only.
    mixed = [_bar(close=100.0, volume=1_000_000.0)] * 19 + [_bar(close=100.0, volume=float("inf"))]
    val = compute_adv_from_bars(mixed, lookback=20)
    assert val == 100_000_000.0
    assert val is not None and math.isfinite(val)


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------


def test_get_named_provider_chain_for_crypto() -> None:
    svc = MarketDataService()
    chain = svc._get_named_provider_chain("crypto")
    slugs = [s for s, _ in chain]
    assert slugs[:3] == ["yahoo", "twelve_data", "coingecko"]


def test_get_named_provider_chain_for_stocks_without_alpha_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without ALPHA_VANTAGE_API_KEY the alphavantage tier is omitted."""
    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "_ALPHA_VANTAGE_API_KEY", "", raising=False)
    chain = MarketDataService()._get_named_provider_chain("stocks")
    slugs = [s for s, _ in chain]
    assert slugs == ["yahoo", "twelve_data"]


def test_get_named_provider_chain_for_stocks_with_alpha_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    chain = MarketDataService()._get_named_provider_chain("stocks")
    slugs = [s for s, _ in chain]
    assert "alphavantage" in slugs


def test_fetch_with_providers_returns_first_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = MarketDataService()

    def _first(_s, _a, _start, _end):
        return [_bar(close=200)], "yahoo"  # bars; slug encoded separately

    def _second(_s, _a, _start, _end):
        return [_bar(close=300)]

    # Stub the named-chain accessor: first provider returns a non-empty list.
    monkeypatch.setattr(
        svc, "_get_named_provider_chain", lambda _: [("yahoo", lambda *a, **k: [_bar(close=200)])]
    )
    bars, slug = svc._fetch_with_providers("AAA", "stocks", "2024-01-01", "2024-01-31")
    assert slug == "yahoo"
    assert bars[0].close == 200


def test_fetch_with_providers_falls_through_to_next_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First provider raises → second wins."""
    svc = MarketDataService()
    calls: List[str] = []

    def _first(*args, **kwargs):
        calls.append("first")
        raise RuntimeError("first failed")

    def _second(*args, **kwargs):
        calls.append("second")
        return [_bar(close=300)]

    monkeypatch.setattr(svc, "_get_named_provider_chain", lambda _: [("a", _first), ("b", _second)])
    bars, slug = svc._fetch_with_providers("AAA", "stocks", "s", "e")
    assert slug == "b"
    assert bars[0].close == 300
    assert calls == ["first", "second"]


def test_fetch_with_providers_returns_empty_when_all_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = MarketDataService()
    monkeypatch.setattr(
        svc,
        "_get_named_provider_chain",
        lambda _: [("a", lambda *a, **k: []), ("b", lambda *a, **k: [])],
    )
    bars, slug = svc._fetch_with_providers("AAA", "stocks", "s", "e")
    assert bars == []
    assert slug == ""


# ---------------------------------------------------------------------------
# _df_to_bars
# ---------------------------------------------------------------------------


def test_df_to_bars_normalises_ohlc_invariants() -> None:
    """When yfinance reports H/L outside the O/C envelope, the helper repairs them."""

    class _Row:
        def __init__(self, data: Dict[str, float]) -> None:
            self._d = data

        def __getitem__(self, key: str) -> float:
            return self._d[key]

        def get(self, key: str, default: float = 0.0) -> float:
            return self._d.get(key, default)

    class _Index:
        def __init__(self, label: str) -> None:
            self._label = label

        def strftime(self, fmt: str) -> str:  # noqa: ARG002 — accept arbitrary fmt
            return self._label

    class _DF:
        def __init__(self, rows: List[Tuple[_Index, _Row]]) -> None:
            self._rows = rows

        def iterrows(self):
            return iter(self._rows)

    # Bar A: H/L outside envelope → must be repaired.
    rows = [
        (
            _Index("2024-01-01"),
            _Row({"Open": 100, "High": 95, "Low": 105, "Close": 102, "Volume": 1000}),
        ),
        (
            _Index("2024-01-02"),
            _Row({"Open": 102, "High": 110, "Low": 100, "Close": 108, "Volume": 2000}),
        ),
    ]
    bars = MarketDataService._df_to_bars(_DF(rows))
    assert len(bars) == 2
    # Repaired: high = max(100, 95, 105, 102) = 105; low = min(...) = 95.
    assert bars[0].high == 105
    assert bars[0].low == 95
    # Volume forwarded.
    assert bars[0].volume == 1000


def test_df_to_bars_filters_nan_rows() -> None:
    """Rows containing NaN in any OHLC field must be dropped.

    yfinance occasionally surfaces NaN rows during data gaps; left unfiltered,
    a NaN close propagates straight into the SpecReadinessGate's market sample
    provider (which then trips the sizing realisability critical with
    ``got nan``). Filter at the data boundary so no downstream consumer ever
    sees a non-finite OHLC value.
    """
    import math

    class _Row:
        def __init__(self, data: Dict[str, float]) -> None:
            self._d = data

        def __getitem__(self, key: str) -> float:
            return self._d[key]

        def get(self, key: str, default: float = 0.0) -> float:
            return self._d.get(key, default)

    class _Index:
        def __init__(self, label: str) -> None:
            self._label = label

        def strftime(self, fmt: str) -> str:  # noqa: ARG002 — accept arbitrary fmt
            return self._label

    class _DF:
        def __init__(self, rows: List[Tuple[_Index, _Row]]) -> None:
            self._rows = rows

        def iterrows(self):
            return iter(self._rows)

    rows = [
        # Good bar — kept.
        (
            _Index("2024-01-01"),
            _Row({"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}),
        ),
        # NaN Close — dropped.
        (
            _Index("2024-01-02"),
            _Row({"Open": 100, "High": 101, "Low": 99, "Close": float("nan"), "Volume": 1000}),
        ),
        # +inf Open — dropped.
        (
            _Index("2024-01-03"),
            _Row({"Open": float("inf"), "High": 101, "Low": 99, "Close": 100, "Volume": 1000}),
        ),
        # Good bar — kept.
        (
            _Index("2024-01-04"),
            _Row({"Open": 102, "High": 103, "Low": 101, "Close": 102, "Volume": 2000}),
        ),
    ]
    bars = MarketDataService._df_to_bars(_DF(rows))
    # Interior non-finite rows are forward-filled (not dropped) so positional
    # indicators keep their calendar alignment.
    assert [b.date for b in bars] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]
    assert [b.is_imputed for b in bars] == [False, True, True, False]
    # Imputed bars carry the prior valid close (100) as a flat, zero-volume bar.
    for b in (bars[1], bars[2]):
        assert b.open == b.high == b.low == b.close == 100
        assert b.volume == 0.0
    assert all(
        math.isfinite(b.open)
        and math.isfinite(b.high)
        and math.isfinite(b.low)
        and math.isfinite(b.close)
        for b in bars
    )


def test_df_to_bars_finite_ohlc_nan_volume_kept() -> None:
    """A row with good OHLC but NaN volume stays a real bar; only volume → 0.0.

    Distinct from the non-finite-OHLC case (which forward-fills): the price
    data is valid, so the bar must NOT be dropped/imputed — indicators index
    OHLC positionally. Only the malformed volume is neutralised.
    """

    class _Row:
        def __init__(self, data: Dict[str, float]) -> None:
            self._d = data

        def __getitem__(self, key: str) -> float:
            return self._d[key]

        def get(self, key: str, default: float = 0.0) -> float:
            return self._d.get(key, default)

    class _Index:
        def __init__(self, label: str) -> None:
            self._label = label

        def strftime(self, fmt: str) -> str:  # noqa: ARG002 — accept arbitrary fmt
            return self._label

    class _DF:
        def __init__(self, rows: List[Tuple[_Index, _Row]]) -> None:
            self._rows = rows

        def iterrows(self):
            return iter(self._rows)

    rows = [
        (
            _Index("2024-01-01"),
            _Row({"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}),
        ),
        # Finite OHLC, NaN volume — kept as a real bar with volume coerced to 0.0.
        (
            _Index("2024-01-02"),
            _Row({"Open": 102, "High": 103, "Low": 101, "Close": 102, "Volume": float("nan")}),
        ),
    ]
    bars = MarketDataService._df_to_bars(_DF(rows))
    assert [b.date for b in bars] == ["2024-01-01", "2024-01-02"]
    # The NaN-volume bar is a real bar (not forward-filled) with its OHLC intact.
    assert bars[1].is_imputed is False
    assert (bars[1].open, bars[1].high, bars[1].low, bars[1].close) == (102, 103, 101, 102)
    assert bars[1].volume == 0.0
    assert all(math.isfinite(b.volume) for b in bars)


class _MockRow:
    """Minimal stand-in for a pandas row: ``row[key]`` + ``row.get(key, default)``."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self._d = data

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, key: str, default: Any = 0.0) -> Any:
        return self._d.get(key, default)


class _MockIndex:
    def __init__(self, label: str) -> None:
        self._label = label

    def strftime(self, fmt: str) -> str:  # noqa: ARG002 — accept arbitrary fmt
        return self._label


class _MockDF:
    def __init__(self, rows: List[Tuple[_MockIndex, _MockRow]]) -> None:
        self._rows = rows

    def iterrows(self):
        return iter(self._rows)


def test_df_to_bars_none_ohlc_forward_filled() -> None:
    """A ``None`` in an OHLC column is a missing-value gap, not a crash.

    Object/nullable dtypes (a pyarrow-backed yfinance frame, a transient
    object-dtype column, or a hand-built adapter frame) carry ``None`` rather
    than ``numpy.nan``. ``float(None)`` raises ``TypeError``; left unguarded it
    aborts the whole fetch. It must instead forward-fill like an upstream NaN.
    """
    rows = [
        (
            _MockIndex("2024-01-01"),
            _MockRow({"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}),
        ),
        # None Close — non-finite gap, forward-filled from the prior close.
        (
            _MockIndex("2024-01-02"),
            _MockRow({"Open": 100, "High": 101, "Low": 99, "Close": None, "Volume": 1000}),
        ),
        (
            _MockIndex("2024-01-03"),
            _MockRow({"Open": 102, "High": 103, "Low": 101, "Close": 102, "Volume": 2000}),
        ),
    ]
    bars = MarketDataService._df_to_bars(_MockDF(rows))
    assert [b.date for b in bars] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert [b.is_imputed for b in bars] == [False, True, False]
    # Imputed bar carries the prior valid close (100) as a flat, zero-volume bar.
    assert bars[1].open == bars[1].high == bars[1].low == bars[1].close == 100
    assert bars[1].volume == 0.0
    assert all(
        math.isfinite(b.open)
        and math.isfinite(b.high)
        and math.isfinite(b.low)
        and math.isfinite(b.close)
        for b in bars
    )


def test_df_to_bars_pd_na_ohlc_forward_filled() -> None:
    """A pandas ``NA`` in an OHLC column forward-fills identically to ``None``.

    This is the nullable/pyarrow-dtype migration scenario: ``float(pd.NA)``
    raises ``TypeError`` exactly like ``float(None)``.
    """
    pd = pytest.importorskip("pandas")
    rows = [
        (
            _MockIndex("2024-01-01"),
            _MockRow({"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}),
        ),
        # pd.NA Open — non-finite gap, forward-filled.
        (
            _MockIndex("2024-01-02"),
            _MockRow({"Open": pd.NA, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}),
        ),
        (
            _MockIndex("2024-01-03"),
            _MockRow({"Open": 102, "High": 103, "Low": 101, "Close": 102, "Volume": 2000}),
        ),
    ]
    bars = MarketDataService._df_to_bars(_MockDF(rows))
    assert [b.date for b in bars] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    assert [b.is_imputed for b in bars] == [False, True, False]
    assert bars[1].open == bars[1].high == bars[1].low == bars[1].close == 100
    assert bars[1].volume == 0.0


def test_df_to_bars_none_volume_kept() -> None:
    """Finite OHLC with a ``None`` volume keeps the real price bar; volume → 0.0.

    Mirrors the NaN-volume case — a missing volume must not drop the bar, since
    indicators index OHLC positionally.
    """
    rows = [
        (
            _MockIndex("2024-01-01"),
            _MockRow({"Open": 100, "High": 101, "Low": 99, "Close": 100, "Volume": 1000}),
        ),
        # Finite OHLC, None volume — kept as a real bar with volume coerced to 0.0.
        (
            _MockIndex("2024-01-02"),
            _MockRow({"Open": 102, "High": 103, "Low": 101, "Close": 102, "Volume": None}),
        ),
    ]
    bars = MarketDataService._df_to_bars(_MockDF(rows))
    assert [b.date for b in bars] == ["2024-01-01", "2024-01-02"]
    assert bars[1].is_imputed is False
    assert (bars[1].open, bars[1].high, bars[1].low, bars[1].close) == (102, 103, 101, 102)
    assert bars[1].volume == 0.0
    assert all(math.isfinite(b.volume) for b in bars)


def test_fetch_yahoo_conversion_error_returns_empty(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A conversion error on a non-empty frame degrades to ``[]`` — never aborts.

    Defense in depth: the price-data fetch must fall through to the next
    provider rather than propagating an unexpected error from frame conversion.
    """

    class _DF:
        empty = False

        def iterrows(self):
            raise RuntimeError("boom")

    class _Ticker:
        def __init__(self, sym):
            self.sym = sym

        def history(self, **kwargs):
            return _DF()

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    with caplog.at_level("WARNING"):
        bars = MarketDataService()._fetch_yahoo(
            "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
        )
    assert bars == []
    assert any("failed to convert yfinance frame" in r.message for r in caplog.records)


def test_df_to_bars_falls_back_when_index_has_no_strftime() -> None:
    """Indexes without strftime are stringified and truncated to 10 chars."""

    class _Row:
        def __getitem__(self, key: str) -> float:
            return {"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0}[key]

        def get(self, key: str, default: float = 0.0) -> float:
            return default

    class _DF:
        def iterrows(self):
            return iter([("2024-02-02T00:00:00Z", _Row())])

    bars = MarketDataService._df_to_bars(_DF())
    assert bars[0].date == "2024-02-02"


# ---------------------------------------------------------------------------
# _fetch_yahoo (yfinance stub)
# ---------------------------------------------------------------------------


def test_fetch_yahoo_returns_empty_when_yfinance_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yfinance", None)
    svc = MarketDataService()
    result = svc._fetch_yahoo("AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1)
    assert result == []


def test_fetch_yahoo_with_stubbed_yfinance(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubbed yfinance returns a non-empty DataFrame → fetcher converts to bars."""

    class _DF:
        empty = False

        def iterrows(self):
            class _Row:
                def __getitem__(self, key):
                    return {"Open": 1.0, "High": 1.5, "Low": 0.5, "Close": 1.2}[key]

                def get(self, key, default=0.0):
                    return 1000.0 if key == "Volume" else default

            class _Idx:
                def strftime(self, fmt):
                    return "2024-01-02"

            return iter([(_Idx(), _Row())])

    class _Ticker:
        def __init__(self, sym):
            self.sym = sym

        def history(self, **kwargs):
            return _DF()

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    bars = MarketDataService()._fetch_yahoo(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert len(bars) == 1
    assert bars[0].date == "2024-01-02"


def test_fetch_yahoo_crypto_maps_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: Dict[str, str] = {}

    class _DF:
        empty = True

        def iterrows(self):
            return iter([])

    class _Ticker:
        def __init__(self, sym):
            captured["sym"] = sym

        def history(self, **kwargs):
            return _DF()

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    MarketDataService()._fetch_yahoo("BTC", "crypto", "2024-01-01", "2024-01-31", max_retries=1)
    # BTC → BTC-USD via YAHOO_CRYPTO_TICKERS.
    assert captured["sym"] == "BTC-USD"


def test_fetch_yahoo_crypto_idempotent_on_provider_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Crypto symbols already in Yahoo form (``ETH-USD``) must NOT have
    ``-USD`` appended a second time.

    Operators and the spec-readiness gate both accept the Yahoo-suffixed
    form as a valid ``target_symbols`` entry; the fetcher must round-trip
    that form unchanged rather than producing ``ETH-USD-USD``, which
    yfinance reports as a delisted symbol after three retries.
    """
    captured: Dict[str, str] = {}

    class _DF:
        empty = True

        def iterrows(self):
            return iter([])

    class _Ticker:
        def __init__(self, sym):
            captured["sym"] = sym

        def history(self, **kwargs):
            return _DF()

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    MarketDataService()._fetch_yahoo("ETH-USD", "crypto", "2024-01-01", "2024-01-31", max_retries=1)
    assert captured["sym"] == "ETH-USD"


def test_fetch_yahoo_crypto_unknown_symbol_appends_usd_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crypto symbol not in ``YAHOO_CRYPTO_TICKERS`` and lacking the
    ``-USD`` suffix still gets the suffix appended exactly once."""
    captured: Dict[str, str] = {}

    class _DF:
        empty = True

        def iterrows(self):
            return iter([])

    class _Ticker:
        def __init__(self, sym):
            captured["sym"] = sym

        def history(self, **kwargs):
            return _DF()

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    # "DOGE" is not in YAHOO_CRYPTO_TICKERS — exercise the fallback path.
    MarketDataService()._fetch_yahoo("DOGE", "crypto", "2024-01-01", "2024-01-31", max_retries=1)
    assert captured["sym"] == "DOGE-USD"


# ---------------------------------------------------------------------------
# _fetch_twelve_data (httpx stub)
# ---------------------------------------------------------------------------


class _StubResp:
    def __init__(self, payload: Dict[str, Any], status: int = 200) -> None:
        self._p = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=None,
                response=_StubHttpResponse(self.status_code),  # type: ignore[arg-type]
            )

    def json(self) -> Dict[str, Any]:
        return self._p


class _StubHttpResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status


def _install_httpx_stub(
    monkeypatch: pytest.MonkeyPatch, responses: Dict[str, Any] | Exception
) -> None:
    """Replace httpx.Client with a context-manager stub returning queued responses."""

    class _StubClient:
        def __init__(self, timeout=None):  # noqa: ARG002
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url: str, params: Optional[Dict[str, Any]] = None) -> _StubResp:
            if isinstance(responses, Exception):
                raise responses
            for substr, resp in responses.items():
                if substr in url:
                    if isinstance(resp, Exception):
                        raise resp
                    return resp
            return _StubResp({})

    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds.httpx, "Client", _StubClient)


def test_fetch_twelve_data_success(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "values": [
            {
                "datetime": "2024-01-02",
                "open": "1.0",
                "high": "1.5",
                "low": "0.5",
                "close": "1.2",
                "volume": "1000",
            }
        ]
    }
    _install_httpx_stub(monkeypatch, {"twelvedata": _StubResp(payload)})
    bars = MarketDataService()._fetch_twelve_data(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert len(bars) == 1
    assert bars[0].close == 1.2


def test_fetch_twelve_data_returns_empty_on_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"status": "error", "message": "bad symbol"}
    _install_httpx_stub(monkeypatch, {"twelvedata": _StubResp(payload)})
    bars = MarketDataService()._fetch_twelve_data(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert bars == []


def test_fetch_twelve_data_returns_empty_on_missing_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx_stub(monkeypatch, {"twelvedata": _StubResp({"values": None})})
    bars = MarketDataService()._fetch_twelve_data(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert bars == []


def test_fetch_twelve_data_repairs_ohlc_invariants(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "values": [
            # High=0.5 but close=1.0 → repair high to max(open, high, low, close).
            {
                "datetime": "2024-01-02",
                "open": "1.0",
                "high": "0.5",
                "low": "1.5",
                "close": "1.0",
                "volume": "100",
            }
        ]
    }
    _install_httpx_stub(monkeypatch, {"twelvedata": _StubResp(payload)})
    bars = MarketDataService()._fetch_twelve_data(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert bars[0].high == 1.5
    assert bars[0].low == 0.5


def test_fetch_twelve_data_drops_nan_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A leading NaN OHLC field from the Twelve Data fallback is dropped.

    After the newest-first payload is reversed to chronological order this NaN
    row is the earliest bar, so there is no prior close to forward-fill from and
    the row is dropped (the only case forward-fill cannot recover)."""
    payload = {
        "values": [
            {
                "datetime": "2024-01-03",
                "open": "1.0",
                "high": "1.5",
                "low": "0.5",
                "close": "1.2",
                "volume": "100",
            },
            # NaN close; chronologically first after reverse -> leading drop.
            {
                "datetime": "2024-01-02",
                "open": "1.0",
                "high": "1.5",
                "low": "0.5",
                "close": "nan",
                "volume": "100",
            },
        ]
    }
    _install_httpx_stub(monkeypatch, {"twelvedata": _StubResp(payload)})
    bars = MarketDataService()._fetch_twelve_data(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert len(bars) == 1
    assert bars[0].date == "2024-01-03"
    assert all(
        math.isfinite(b.open)
        and math.isfinite(b.high)
        and math.isfinite(b.low)
        and math.isfinite(b.close)
        for b in bars
    )


def test_fetch_twelve_data_swallows_generic_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx_stub(monkeypatch, RuntimeError("boom"))
    bars = MarketDataService()._fetch_twelve_data(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert bars == []


# ---------------------------------------------------------------------------
# _fetch_coingecko
# ---------------------------------------------------------------------------


def test_fetch_coingecko_returns_empty_for_non_crypto(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = MarketDataService()._fetch_coingecko(
        "AAA", "stocks", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert bars == []


def test_fetch_coingecko_returns_empty_for_unknown_coin(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = MarketDataService()._fetch_coingecko(
        "XYZ", "crypto", "2024-01-01", "2024-01-31", max_retries=1
    )
    assert bars == []


def test_fetch_coingecko_returns_empty_for_bad_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    bars = MarketDataService()._fetch_coingecko(
        "BTC", "crypto", "bad-date", "2024-01-31", max_retries=1
    )
    assert bars == []


def test_fetch_coingecko_success(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two price ticks on the same day; the daily bar uses first/min/max/last.
    payload = {
        "prices": [
            [1704153600000, 100.0],  # 2024-01-02 00:00
            [1704196800000, 102.0],  # 2024-01-02 12:00
            [1704240000000, 101.0],  # 2024-01-03 00:00
        ]
    }
    _install_httpx_stub(monkeypatch, {"coingecko": _StubResp(payload)})
    bars = MarketDataService()._fetch_coingecko(
        "BTC", "crypto", "2024-01-02", "2024-01-04", max_retries=1
    )
    assert len(bars) >= 1
    # Volume defaults to 0.0 for the synthesised OHLCV.
    assert all(b.volume == 0.0 for b in bars)


def test_fetch_coingecko_forward_fills_nan_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NaN price in the CoinGecko series is forward-filled (carries the prior
    close) rather than dropped, so the series stays calendar-aligned."""
    payload = {
        "prices": [
            [1704153600000, 100.0],  # 2024-01-02 — finite, kept
            [1704240000000, float("nan")],  # 2024-01-03 — NaN, forward-filled
        ]
    }
    _install_httpx_stub(monkeypatch, {"coingecko": _StubResp(payload)})
    bars = MarketDataService()._fetch_coingecko(
        "BTC", "crypto", "2024-01-02", "2024-01-04", max_retries=1
    )
    # The trailing NaN day is forward-filled (not dropped), carrying the prior
    # close so the series stays calendar-aligned.
    assert [b.date for b in bars] == ["2024-01-02", "2024-01-03"]
    assert [b.is_imputed for b in bars] == [False, True]
    assert bars[1].close == 100.0
    assert bars[1].volume == 0.0
    assert all(math.isfinite(b.close) for b in bars)


def test_fetch_coingecko_returns_empty_on_unexpected_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_httpx_stub(monkeypatch, {"coingecko": _StubResp({"unexpected": True})})
    bars = MarketDataService()._fetch_coingecko(
        "BTC", "crypto", "2024-01-02", "2024-01-04", max_retries=1
    )
    assert bars == []


def test_fetch_coingecko_swallows_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_httpx_stub(monkeypatch, RuntimeError("boom"))
    bars = MarketDataService()._fetch_coingecko(
        "BTC", "crypto", "2024-01-02", "2024-01-04", max_retries=1
    )
    assert bars == []


# ---------------------------------------------------------------------------
# _fetch_alphavantage
# ---------------------------------------------------------------------------


def test_fetch_alphavantage_returns_empty_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(mds, "_ALPHA_VANTAGE_API_KEY", "", raising=False)
    bars = MarketDataService()._fetch_alphavantage("AAA", "stocks", "2024-01-01", "2024-01-31")
    assert bars == []


def test_fetch_alphavantage_handles_error_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    _install_httpx_stub(monkeypatch, {"alphavantage": _StubResp({"Error Message": "bad"})})
    bars = MarketDataService()._fetch_alphavantage("AAA", "stocks", "2024-01-01", "2024-01-31")
    assert bars == []


def test_fetch_alphavantage_handles_missing_ts(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    _install_httpx_stub(monkeypatch, {"alphavantage": _StubResp({"Meta Data": {}})})
    bars = MarketDataService()._fetch_alphavantage("AAA", "stocks", "2024-01-01", "2024-01-31")
    assert bars == []


def test_fetch_alphavantage_stocks_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    payload = {
        "Time Series (Daily)": {
            "2024-01-15": {
                "1. open": "100",
                "2. high": "101",
                "3. low": "99",
                "4. close": "100.5",
                "5. volume": "500000",
            },
            # Out-of-range date — must be filtered out.
            "2025-06-01": {
                "1. open": "200",
                "2. high": "201",
                "3. low": "199",
                "4. close": "200.5",
                "5. volume": "100000",
            },
        }
    }
    _install_httpx_stub(monkeypatch, {"alphavantage": _StubResp(payload)})
    bars = MarketDataService()._fetch_alphavantage("AAA", "stocks", "2024-01-01", "2024-01-31")
    assert len(bars) == 1
    assert bars[0].date == "2024-01-15"
    assert bars[0].close == 100.5


def test_fetch_alphavantage_forex_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    payload = {
        "Time Series FX (Daily)": {
            "2024-01-15": {"1. open": "1.0", "2. high": "1.1", "3. low": "0.9", "4. close": "1.05"}
        }
    }
    _install_httpx_stub(monkeypatch, {"alphavantage": _StubResp(payload)})
    bars = MarketDataService()._fetch_alphavantage("EURUSD", "forex", "2024-01-01", "2024-01-31")
    assert len(bars) == 1


def test_fetch_alphavantage_forward_fills_nan_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """A NaN OHLC field from the Alpha Vantage fallback is forward-filled
    (carries the prior close), matching yfinance and Twelve Data."""
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    payload = {
        "Time Series (Daily)": {
            "2024-01-15": {
                "1. open": "100",
                "2. high": "101",
                "3. low": "99",
                "4. close": "100.5",
                "5. volume": "500000",
            },
            # NaN close → row is forward-filled (carries the prior close).
            "2024-01-16": {
                "1. open": "100",
                "2. high": "101",
                "3. low": "99",
                "4. close": "nan",
                "5. volume": "500000",
            },
        }
    }
    _install_httpx_stub(monkeypatch, {"alphavantage": _StubResp(payload)})
    bars = MarketDataService()._fetch_alphavantage("AAA", "stocks", "2024-01-01", "2024-01-31")
    # The trailing NaN day is forward-filled (not dropped), carrying the prior
    # close so the series stays calendar-aligned.
    assert [b.date for b in bars] == ["2024-01-15", "2024-01-16"]
    assert [b.is_imputed for b in bars] == [False, True]
    assert bars[1].close == 100.5
    assert bars[1].volume == 0.0
    assert all(math.isfinite(b.close) for b in bars)


# ---------------------------------------------------------------------------
# _normalize_ohlc_bar (shared across all provider paths)
# ---------------------------------------------------------------------------


def test_normalize_ohlc_bar_returns_none_for_non_finite() -> None:
    bar, repaired = MarketDataService._normalize_ohlc_bar(
        date="2024-01-02", open=1.0, high=1.5, low=0.5, close=float("nan"), volume=10.0
    )
    assert bar is None
    assert repaired is False


def test_normalize_ohlc_bar_repairs_envelope() -> None:
    # high reported below the true max and low above the true min → repaired.
    bar, repaired = MarketDataService._normalize_ohlc_bar(
        date="2024-01-02", open=1.0, high=0.5, low=1.5, close=1.0, volume=10.0
    )
    assert bar is not None
    assert repaired is True
    assert bar.high == 1.5
    assert bar.low == 0.5


def test_normalize_ohlc_bar_clean_row_not_flagged_repaired() -> None:
    bar, repaired = MarketDataService._normalize_ohlc_bar(
        date="2024-01-02", open=1.0, high=1.5, low=0.5, close=1.2, volume=10.0
    )
    assert bar is not None
    assert repaired is False
    assert bar.high == 1.5
    assert bar.low == 0.5
    assert bar.volume == 10.0


@pytest.mark.parametrize("bad_volume", [float("nan"), float("inf"), float("-inf")])
def test_normalize_ohlc_bar_coerces_nonfinite_volume(bad_volume: float, caplog) -> None:
    """Finite OHLC with non-finite volume keeps the price bar; volume → 0.0."""
    with caplog.at_level("WARNING"):
        bar, repaired = MarketDataService._normalize_ohlc_bar(
            date="2024-01-02", open=1.0, high=1.5, low=0.5, close=1.2, volume=bad_volume
        )
    # The real price bar is preserved (not dropped) — only volume is neutralised.
    assert bar is not None
    assert repaired is False
    assert bar.high == 1.5
    assert bar.low == 0.5
    assert bar.volume == 0.0
    assert math.isfinite(bar.volume)
    assert any(
        "non-finite volume" in r.message and "2024-01-02" in r.message for r in caplog.records
    )


def _missing_sentinels() -> List[Any]:
    """``None`` plus pandas ``NA`` when pandas is importable."""
    sentinels: List[Any] = [None]
    try:
        import pandas as pd

        sentinels.append(pd.NA)
    except ImportError:  # pragma: no cover - pandas is a transitive dep
        pass
    return sentinels


@pytest.mark.parametrize("sentinel", _missing_sentinels())
def test_normalize_ohlc_bar_missing_ohlc_is_dropped(sentinel: Any) -> None:
    """A missing-value sentinel (``None`` / ``pd.NA``) in OHLC → ``(None, False)``.

    The row is reported as a non-finite gap (the caller forward-fills/drops it)
    rather than raising ``TypeError`` from ``float(sentinel)``.
    """
    bar, repaired = MarketDataService._normalize_ohlc_bar(
        date="2024-01-02", open=sentinel, high=1.5, low=0.5, close=1.2, volume=10.0
    )
    assert bar is None
    assert repaired is False


@pytest.mark.parametrize("sentinel", _missing_sentinels())
def test_normalize_ohlc_bar_missing_volume_coerced(sentinel: Any) -> None:
    """A missing-value sentinel volume with finite OHLC keeps the bar; volume → 0.0."""
    bar, repaired = MarketDataService._normalize_ohlc_bar(
        date="2024-01-02", open=1.0, high=1.5, low=0.5, close=1.2, volume=sentinel
    )
    assert bar is not None
    assert bar.volume == 0.0
    assert math.isfinite(bar.volume)


def test_normalize_ohlc_bar_malformed_string_still_raises() -> None:
    """A genuinely non-numeric string is still a caller bug → ``ValueError``.

    Only missing-value sentinels (TypeError on ``float()``) are tolerated; a
    malformed numeric string raises ``ValueError`` and must propagate.
    """
    with pytest.raises(ValueError):
        MarketDataService._normalize_ohlc_bar(
            date="2024-01-02", open="abc", high=1.5, low=0.5, close=1.2, volume=10.0
        )


def test_fetch_alphavantage_crypto_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    payload = {
        "Time Series (Digital Currency Daily)": {
            "2024-01-15": {
                "1a. open (USD)": "50000",
                "2a. high (USD)": "51000",
                "3a. low (USD)": "49000",
                "4a. close (USD)": "50500",
                "5. market cap (USD)": "1000000",
            }
        }
    }
    _install_httpx_stub(monkeypatch, {"alphavantage": _StubResp(payload)})
    bars = MarketDataService()._fetch_alphavantage("BTC", "crypto", "2024-01-01", "2024-01-31")
    assert len(bars) == 1
    assert bars[0].close == 50500


def test_fetch_alphavantage_swallows_generic_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    import investment_team.market_data_service as mds

    monkeypatch.setattr(
        mds, "_ALPHA_VANTAGE_API_KEY", "fixture-placeholder-not-a-secret", raising=False
    )
    _install_httpx_stub(monkeypatch, RuntimeError("network"))
    bars = MarketDataService()._fetch_alphavantage("AAA", "stocks", "2024-01-01", "2024-01-31")
    assert bars == []


# ---------------------------------------------------------------------------
# _warn_on_asset_class_mismatch
# ---------------------------------------------------------------------------


def test_warn_on_asset_class_mismatch_warns_for_unambiguous_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from investment_team.models import StrategySpec

    svc = MarketDataService()
    # BTC is unambiguously crypto via classify_symbol; declaring stocks → mismatch.
    spec = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        target_symbols=["BTC"],
    )
    with caplog.at_level(logging.WARNING, logger="investment_team.market_data_service"):
        svc._warn_on_asset_class_mismatch(spec)
    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "do not match asset_class" in msgs


def test_warn_on_asset_class_mismatch_silent_when_matches() -> None:
    """Targets matching the declared asset class — no warning, no exception."""
    from investment_team.models import StrategySpec

    svc = MarketDataService()
    spec = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="crypto",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        target_symbols=["BTC", "ETH"],
    )
    # Should not raise.
    svc._warn_on_asset_class_mismatch(spec)


# ---------------------------------------------------------------------------
# resolve_strategy_symbols + get_symbols_for_strategy
# ---------------------------------------------------------------------------


def test_resolve_strategy_symbols_returns_targets_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.models import StrategySpec

    svc = MarketDataService()
    spec = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
        target_symbols=["AAPL", "MSFT"],
    )
    assert svc.resolve_strategy_symbols(spec) == ["AAPL", "MSFT"]


def test_resolve_strategy_symbols_truncates_default_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_team.models import StrategySpec

    # Cap at 2 — the asset-class default for stocks is much longer.
    monkeypatch.setenv("STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS", "2")

    svc = MarketDataService()
    spec = StrategySpec(
        strategy_id="s",
        authored_by="x",
        asset_class="stocks",
        hypothesis="h",
        signal_definition="s",
        timeframe="1d",
    )
    out = svc.resolve_strategy_symbols(spec)
    assert len(out) == 2


# ---------------------------------------------------------------------------
# canonical_symbol + crypto -USD normalization across the provider chain
# ---------------------------------------------------------------------------


def test_canonical_symbol_strips_usd_suffix_for_crypto() -> None:
    from investment_team.symbols import canonical_symbol

    assert canonical_symbol("BTC-USD", "crypto") == "BTC"
    assert canonical_symbol("btc-usd", "crypto") == "BTC"


def test_canonical_symbol_is_identity_for_bare_crypto_alias() -> None:
    from investment_team.symbols import canonical_symbol

    assert canonical_symbol("BTC", "crypto") == "BTC"


def test_canonical_symbol_is_idempotent() -> None:
    from investment_team.symbols import canonical_symbol

    once = canonical_symbol("ETH-USD", "crypto")
    assert canonical_symbol(once, "crypto") == once == "ETH"


def test_canonical_symbol_leaves_non_crypto_untouched() -> None:
    from investment_team.symbols import canonical_symbol

    # Equities/forex/futures keep their exact spelling (incl. case + suffix).
    assert canonical_symbol("AAPL", "stocks") == "AAPL"
    assert canonical_symbol("EURUSD=X", "forex") == "EURUSD=X"
    assert canonical_symbol("ES=F", "futures") == "ES=F"


def test_crypto_usd_suffix_resolves_to_same_provider_tickers_as_bare_alias() -> None:
    """Acceptance: ``BTC-USD`` and ``BTC`` produce identical provider tickers.

    Twelve Data must see ``BTC/USD`` and Alpha Vantage must see bare ``BTC``
    regardless of which spelling the spec used.
    """
    from investment_team.data_providers.symbol_maps import resolve_twelve_data
    from investment_team.symbols import canonical_symbol

    suffixed = canonical_symbol("BTC-USD", "crypto")
    bare = canonical_symbol("BTC", "crypto")

    # Twelve Data ticker (resolver maps the canonical alias).
    assert (
        resolve_twelve_data(suffixed, "crypto") == resolve_twelve_data(bare, "crypto") == "BTC/USD"
    )
    # Alpha Vantage passes ``symbol.upper()`` verbatim for crypto → bare BTC.
    assert suffixed.upper() == bare.upper() == "BTC"


def test_fetch_with_providers_normalizes_crypto_symbol_for_every_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The chokepoint passes the canonical bare alias to every provider.

    Simulates Yahoo falling through (empty) so the symbol the downstream
    Twelve Data / Alpha Vantage providers receive is observable.
    """
    svc = MarketDataService()
    seen: List[str] = []

    def _record(sym: str, _ac: str, _start: str, _end: str) -> List[OHLCVBar]:
        seen.append(sym)
        return []  # force fall-through to the next provider

    monkeypatch.setattr(
        svc,
        "_get_named_provider_chain",
        lambda _: [("yahoo", _record), ("twelve_data", _record), ("coingecko", _record)],
    )
    svc._fetch_with_providers("BTC-USD", "crypto", "2024-01-01", "2024-01-31")
    assert seen == ["BTC", "BTC", "BTC"]


def test_forward_fill_carries_prev_close() -> None:
    """An interior non-finite row becomes a flat imputed bar at the prior close."""
    good = MarketDataService._normalize_ohlc_bar(
        date="2024-01-01", open=10.0, high=11.0, low=9.0, close=10.5, volume=5.0
    )[0]
    later = MarketDataService._normalize_ohlc_bar(
        date="2024-01-03", open=12.0, high=13.0, low=11.0, close=12.5, volume=7.0
    )[0]
    rows = [("2024-01-01", good), ("2024-01-02", None), ("2024-01-03", later)]

    bars = MarketDataService._forward_fill_bars(rows, provider="test", symbol="X")

    assert [b.date for b in bars] == ["2024-01-01", "2024-01-02", "2024-01-03"]
    imputed = bars[1]
    assert imputed.is_imputed is True
    assert imputed.open == imputed.high == imputed.low == imputed.close == 10.5
    assert imputed.volume == 0.0
    assert bars[0].is_imputed is False and bars[2].is_imputed is False


def test_forward_fill_drops_leading_nonfinite(caplog) -> None:
    """Leading non-finite rows have no prior bar to carry forward, so they drop."""
    good = MarketDataService._normalize_ohlc_bar(
        date="2024-01-02", open=10.0, high=11.0, low=9.0, close=10.5, volume=5.0
    )[0]
    rows = [("2024-01-01", None), ("2024-01-02", good)]

    with caplog.at_level("WARNING"):
        bars = MarketDataService._forward_fill_bars(rows, provider="test", symbol="X")

    assert [b.date for b in bars] == ["2024-01-02"]
    assert any("dropped 1 leading non-finite" in r.message for r in caplog.records)
    assert any("2024-01-01" in r.message for r in caplog.records)


def test_forward_fill_preserves_calendar_length() -> None:
    """Output length equals input minus only the unfillable leading drops."""
    first = MarketDataService._normalize_ohlc_bar(
        date="2024-01-01", open=10.0, high=11.0, low=9.0, close=10.5, volume=5.0
    )[0]
    last = MarketDataService._normalize_ohlc_bar(
        date="2024-01-04", open=12.0, high=13.0, low=11.0, close=12.5, volume=7.0
    )[0]
    rows = [
        ("2024-01-01", first),
        ("2024-01-02", None),
        ("2024-01-03", None),
        ("2024-01-04", last),
    ]
    bars = MarketDataService._forward_fill_bars(rows, provider="test", symbol="X")
    assert len(bars) == 4
    assert [b.date for b in bars] == [
        "2024-01-01",
        "2024-01-02",
        "2024-01-03",
        "2024-01-04",
    ]


def test_forward_fill_logging_names_dates_bounded(caplog) -> None:
    """The impute warning names affected dates, truncating past the cap with '...'."""
    good = MarketDataService._normalize_ohlc_bar(
        date="2024-01-01", open=10.0, high=11.0, low=9.0, close=10.5, volume=5.0
    )[0]
    n = MarketDataService._FILL_LOG_MAX_DATES
    rows = [("2024-01-01", good)]
    rows += [(f"2024-02-{i:02d}", None) for i in range(1, n + 3)]  # > cap imputed

    with caplog.at_level("WARNING"):
        MarketDataService._forward_fill_bars(rows, provider="test", symbol="X")

    msg = next(r.message for r in caplog.records if "forward-filled" in r.message)
    assert "..." in msg
    assert msg.count("2024-02-") == n  # only the first N dates are listed


def test_forward_fill_empty_input_returns_empty() -> None:
    """An empty row list yields no bars and no log noise."""
    assert MarketDataService._forward_fill_bars([], provider="test", symbol="X") == []


def test_compute_adv_excludes_imputed_bars() -> None:
    """Imputed (forward-filled, volume=0) bars do not count toward the lookback.

    A window padded out to ``lookback`` by synthetic forward-fills must not
    satisfy the size requirement — otherwise a symbol with only a handful of
    real trading days would report an ADV it never actually traded. This
    restores the pre-forward-fill behaviour where the underlying NaN rows were
    dropped and thus never counted.
    """
    real = [
        OHLCVBar(date=f"2024-01-{i:02d}", open=100, high=100, low=100, close=100, volume=1_000_000)
        for i in (1, 2)
    ]
    imputed = [
        OHLCVBar(
            date=f"2024-02-{i:02d}",
            open=100,
            high=100,
            low=100,
            close=100,
            volume=0.0,
            is_imputed=True,
        )
        for i in range(1, 19)
    ]
    # 2 real + 18 imputed == 20 rows, but only 2 real bars -> below lookback.
    assert compute_adv_from_bars(real + imputed, lookback=20) is None
    # 20 real bars still compute normally.
    full = [
        OHLCVBar(date=f"2024-03-{i:02d}", open=100, high=100, low=100, close=100, volume=1_000_000)
        for i in range(1, 21)
    ]
    assert compute_adv_from_bars(full, lookback=20) == 100_000_000.0


def test_fetch_coingecko_buckets_ticks_by_utc_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """CoinGecko epoch-ms ticks are bucketed by UTC calendar day, host-tz-independent.

    A tick at 2024-01-03T00:30:00Z must land on 2024-01-03 regardless of the
    process timezone. The naive ``date.fromtimestamp`` previously used local
    time, shifting near-midnight ticks across day boundaries and making bars
    non-deterministic across hosts/CI runners.
    """
    import time as _time
    from datetime import datetime, timezone

    ts_ms = int(datetime(2024, 1, 3, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
    payload = {"prices": [[ts_ms, 100.0]]}

    # Force a non-UTC local timezone so a naive conversion would bucket to Jan 2.
    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(_time, "tzset"):
        _time.tzset()
    try:
        _install_httpx_stub(monkeypatch, {"coingecko": _StubResp(payload)})
        bars = MarketDataService()._fetch_coingecko(
            "BTC", "crypto", "2024-01-02", "2024-01-04", max_retries=1
        )
    finally:
        monkeypatch.delenv("TZ", raising=False)
        if hasattr(_time, "tzset"):
            _time.tzset()

    assert [b.date for b in bars] == ["2024-01-03"]


def test_trailing_real_bars_skips_imputed_and_preserves_order() -> None:
    """trailing_real_bars returns the most recent ``lookback`` non-imputed bars
    in chronological order; imputed bars are skipped and do not count."""
    real = [
        OHLCVBar(date=f"2024-01-{i:02d}", open=100, high=100, low=100, close=100, volume=1_000_000)
        for i in range(1, 6)
    ]
    # Interleave an imputed bar that must be skipped, not counted.
    imputed = OHLCVBar(
        date="2024-01-06",
        open=100,
        high=100,
        low=100,
        close=100,
        volume=0.0,
        is_imputed=True,
    )
    bars = real[:3] + [imputed] + real[3:]
    window = trailing_real_bars(bars, 3)
    assert [b.date for b in window] == ["2024-01-03", "2024-01-04", "2024-01-05"]
    assert all(not b.is_imputed for b in window)


def test_trailing_real_bars_empty_when_insufficient_or_nonpositive() -> None:
    """Returns [] when fewer than ``lookback`` real bars exist or lookback <= 0."""
    real = [
        OHLCVBar(date=f"2024-01-{i:02d}", open=100, high=100, low=100, close=100, volume=1_000_000)
        for i in range(1, 4)
    ]
    assert trailing_real_bars(real, 5) == []
    assert trailing_real_bars(real, 0) == []
    assert trailing_real_bars([], 1) == []


def test_trailing_real_bars_selects_trailing_real_across_imputed_gap() -> None:
    """The trailing window is the most recent ``lookback`` *real* bars no matter
    where the imputed bars sit — a block of imputed bars between two runs of real
    bars must not shift which real bars are selected. This pins the invariant
    that skipping imputed during the reverse scan is equivalent to filtering
    them out first, so passing unfiltered bars to compute_adv_from_bars matches
    pre-filtering them (the liquidity gate relies on this equivalence)."""
    block1 = [
        OHLCVBar(date=f"2024-01-{i:02d}", open=100, high=100, low=100, close=100, volume=1.0)
        for i in range(1, 16)
    ]  # 15 real
    gap = [
        OHLCVBar(
            date=f"2024-02-{i:02d}",
            open=100,
            high=100,
            low=100,
            close=100,
            volume=0.0,
            is_imputed=True,
        )
        for i in range(1, 6)
    ]  # 5 imputed between the two real runs
    block2 = [
        OHLCVBar(date=f"2024-03-{i:02d}", open=100, high=100, low=100, close=100, volume=1000.0)
        for i in range(1, 11)
    ]  # 10 real
    bars = block1 + gap + block2

    window = trailing_real_bars(bars, 20)
    assert len(window) == 20
    assert all(not b.is_imputed for b in window)
    # The most recent 20 real bars: last 10 of block1 + all of block2, in order.
    expected = [b.date for b in block1[5:]] + [b.date for b in block2]
    assert [b.date for b in window] == expected

    # Equivalence: passing unfiltered bars equals pre-filtering imputed ones.
    prefiltered = [b for b in bars if not b.is_imputed]
    assert compute_adv_from_bars(bars, lookback=20) == compute_adv_from_bars(
        prefiltered, lookback=20
    )
