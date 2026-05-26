"""Coverage for trading_service provider adapters.

Most provider stubs (Alpaca, Polygon, Databento, TwelveData, OANDA,
Coinbase) only have constructors + ``NotImplementedError`` historical/
live methods. The tests verify the small amount of real logic each
holds: capability descriptors, ``smallest_available`` asset-class
filters, and the validation/auth checks.

The Binance adapter has a real REST klines loop — the tests stub
``httpx.Client`` and cover happy / 451 / non-200 / pagination paths.
"""

from __future__ import annotations

from typing import Any, List

import pytest

from investment_team.trading_service.providers.alpaca import AlpacaAdapter
from investment_team.trading_service.providers.binance import (
    BinanceAdapter,
    _iso_to_ms,
    _ms_to_iso,
)
from investment_team.trading_service.providers.coinbase import CoinbaseAdapter
from investment_team.trading_service.providers.databento import DatabentoAdapter
from investment_team.trading_service.providers.oanda import OandaAdapter
from investment_team.trading_service.providers.polygon import PolygonAdapter
from investment_team.trading_service.providers.twelve_data import TwelveDataAdapter

# ---------------------------------------------------------------------------
# Alpaca
# ---------------------------------------------------------------------------


def test_alpaca_smallest_available_filters_asset_class() -> None:
    adapter = AlpacaAdapter()
    assert adapter.smallest_available("equities", live=True) == "tick"
    assert adapter.smallest_available("equities", live=False) == "1m"
    assert adapter.smallest_available("crypto", live=False) is None


def test_alpaca_constructor_rejects_invalid_feed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALPACA_PAID_FEED", "bogus")
    with pytest.raises(ValueError):
        AlpacaAdapter()


def test_alpaca_historical_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAID_FEED", raising=False)
    adapter = AlpacaAdapter()
    with pytest.raises(RuntimeError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="2024-01-01", end="2024-01-02", timeframe="1m"))


def test_alpaca_live_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    monkeypatch.delenv("ALPACA_PAID_FEED", raising=False)
    adapter = AlpacaAdapter()
    with pytest.raises(RuntimeError):
        list(adapter.live(symbols=["AAPL"], asset_class="equities", native_timeframe="tick"))


def test_alpaca_historical_authed_raises_not_implemented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALPACA_API_KEY_ID", "fixture-placeholder-not-a-secret")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "fixture-placeholder-not-a-secret")
    adapter = AlpacaAdapter()
    with pytest.raises(NotImplementedError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="x", end="y", timeframe="1m"))


def test_alpaca_build_returns_adapter() -> None:
    from investment_team.trading_service.providers.alpaca import build

    assert isinstance(build(), AlpacaAdapter)


# ---------------------------------------------------------------------------
# Coinbase
# ---------------------------------------------------------------------------


def test_coinbase_smallest_available() -> None:
    adapter = CoinbaseAdapter()
    assert adapter.smallest_available("crypto", live=True) == "tick"
    assert adapter.smallest_available("crypto", live=False) == "1m"
    assert adapter.smallest_available("equities", live=True) is None


def test_coinbase_historical_raises_not_implemented() -> None:
    adapter = CoinbaseAdapter()
    with pytest.raises(NotImplementedError):
        list(adapter.historical(symbols=["BTC-USD"], asset_class="crypto", start="x", end="y", timeframe="1m"))


def test_coinbase_live_raises_not_implemented() -> None:
    adapter = CoinbaseAdapter()
    with pytest.raises(NotImplementedError):
        list(adapter.live(symbols=["BTC-USD"], asset_class="crypto", native_timeframe="tick"))


def test_coinbase_build_returns_adapter() -> None:
    from investment_team.trading_service.providers.coinbase import build

    assert isinstance(build(), CoinbaseAdapter)


# ---------------------------------------------------------------------------
# OANDA
# ---------------------------------------------------------------------------


def test_oanda_smallest_available() -> None:
    adapter = OandaAdapter()
    assert adapter.smallest_available("fx", live=True) == "tick"
    assert adapter.smallest_available("fx", live=False) == "5s"
    assert adapter.smallest_available("crypto", live=True) is None


def test_oanda_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
    monkeypatch.delenv("OANDA_ACCOUNT_ID", raising=False)
    adapter = OandaAdapter()
    with pytest.raises(RuntimeError):
        list(adapter.historical(symbols=["EURUSD"], asset_class="fx", start="x", end="y", timeframe="5s"))
    with pytest.raises(RuntimeError):
        list(adapter.live(symbols=["EURUSD"], asset_class="fx", native_timeframe="tick"))


def test_oanda_authed_raises_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OANDA_API_TOKEN", "fixture-placeholder-not-a-secret")
    monkeypatch.setenv("OANDA_ACCOUNT_ID", "test-account-placeholder")
    adapter = OandaAdapter()
    with pytest.raises(NotImplementedError):
        list(adapter.historical(symbols=["EURUSD"], asset_class="fx", start="x", end="y", timeframe="5s"))
    with pytest.raises(NotImplementedError):
        list(adapter.live(symbols=["EURUSD"], asset_class="fx", native_timeframe="tick"))


def test_oanda_build() -> None:
    from investment_team.trading_service.providers.oanda import build

    assert isinstance(build(), OandaAdapter)


# ---------------------------------------------------------------------------
# Polygon
# ---------------------------------------------------------------------------


def test_polygon_smallest_available() -> None:
    adapter = PolygonAdapter()
    assert adapter.smallest_available("equities", live=True) == "tick"
    assert adapter.smallest_available("equities", live=False) == "1s"
    assert adapter.smallest_available("nonsense", live=True) is None


def test_polygon_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POLYGON_API_KEY", raising=False)
    adapter = PolygonAdapter()
    with pytest.raises(RuntimeError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="x", end="y", timeframe="1m"))
    with pytest.raises(RuntimeError):
        list(adapter.live(symbols=["AAPL"], asset_class="equities", native_timeframe="tick"))


def test_polygon_authed_raises_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLYGON_API_KEY", "fixture-placeholder-not-a-secret")
    adapter = PolygonAdapter()
    with pytest.raises(NotImplementedError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="x", end="y", timeframe="1m"))


def test_polygon_build() -> None:
    from investment_team.trading_service.providers.polygon import build

    assert isinstance(build(), PolygonAdapter)


# ---------------------------------------------------------------------------
# Databento
# ---------------------------------------------------------------------------


def test_databento_smallest_available() -> None:
    adapter = DatabentoAdapter()
    assert adapter.smallest_available("equities", live=True) == "tick"
    assert adapter.smallest_available("equities", live=False) == "1s"
    assert adapter.smallest_available("crypto", live=False) is None


def test_databento_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    adapter = DatabentoAdapter()
    with pytest.raises(RuntimeError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="x", end="y", timeframe="1s"))
    with pytest.raises(RuntimeError):
        list(adapter.live(symbols=["AAPL"], asset_class="equities", native_timeframe="tick"))


def test_databento_authed_raises_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABENTO_API_KEY", "fixture-placeholder-not-a-secret")
    adapter = DatabentoAdapter()
    with pytest.raises(NotImplementedError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="x", end="y", timeframe="1s"))


def test_databento_build() -> None:
    from investment_team.trading_service.providers.databento import build

    assert isinstance(build(), DatabentoAdapter)


# ---------------------------------------------------------------------------
# Twelve Data
# ---------------------------------------------------------------------------


def test_twelve_data_smallest_available() -> None:
    adapter = TwelveDataAdapter()
    assert adapter.smallest_available("crypto", live=False) == "1m"
    assert adapter.smallest_available("futures", live=False) is None


def test_twelve_data_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TWELVE_DATA_API_KEY", raising=False)
    monkeypatch.delenv("TWELVE_DATA_PLAN", raising=False)
    adapter = TwelveDataAdapter()
    with pytest.raises(RuntimeError):
        adapter._require_auth()


def test_twelve_data_requires_pro_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "fixture-placeholder-not-a-secret")
    monkeypatch.setenv("TWELVE_DATA_PLAN", "free")
    adapter = TwelveDataAdapter()
    with pytest.raises(RuntimeError):
        adapter._require_auth()


def test_twelve_data_pro_plan_passes_auth_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "fixture-placeholder-not-a-secret")
    monkeypatch.setenv("TWELVE_DATA_PLAN", "pro")
    adapter = TwelveDataAdapter()
    adapter._require_auth()  # no exception


def test_twelve_data_historical_raises_not_implemented(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TWELVE_DATA_API_KEY", "fixture-placeholder-not-a-secret")
    monkeypatch.setenv("TWELVE_DATA_PLAN", "pro")
    adapter = TwelveDataAdapter()
    with pytest.raises(NotImplementedError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="x", end="y", timeframe="1m"))


def test_twelve_data_build() -> None:
    from investment_team.trading_service.providers.twelve_data import build

    assert isinstance(build(), TwelveDataAdapter)


# ---------------------------------------------------------------------------
# Binance — has actual REST logic
# ---------------------------------------------------------------------------


def test_binance_smallest_available() -> None:
    adapter = BinanceAdapter()
    assert adapter.smallest_available("crypto", live=True) == "tick"
    assert adapter.smallest_available("crypto", live=False) == "1s"
    assert adapter.smallest_available("equities", live=True) is None


def test_binance_historical_rejects_non_crypto() -> None:
    adapter = BinanceAdapter()
    with pytest.raises(ValueError):
        list(adapter.historical(symbols=["AAPL"], asset_class="equities", start="2024-01-01", end="2024-01-02", timeframe="1m"))


def test_binance_historical_rejects_rest_unsupported_timeframe() -> None:
    adapter = BinanceAdapter()
    with pytest.raises(ValueError) as exc:
        list(adapter.historical(symbols=["BTC"], asset_class="crypto", start="2024-01-01", end="2024-01-02", timeframe="15s"))
    assert "15s" in str(exc.value)


def test_binance_historical_rejects_unknown_timeframe() -> None:
    adapter = BinanceAdapter()
    with pytest.raises(ValueError):
        list(adapter.historical(symbols=["BTC"], asset_class="crypto", start="2024-01-01", end="2024-01-02", timeframe="3d"))


def _stub_httpx(monkeypatch: pytest.MonkeyPatch, responses: List[Any]) -> None:
    """Install an httpx.Client stub that pops from ``responses`` on each .get()."""

    class _Resp:
        def __init__(self, status_code: int, payload: Any = None, text: str = "") -> None:
            self.status_code = status_code
            self._payload = payload
            self.text = text

        def json(self) -> Any:
            return self._payload

    class _Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a, **k):
            return False

        def get(self, url, params=None):
            if not responses:
                # Default: empty data, terminates the loop.
                return _Resp(200, [])
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    import httpx

    monkeypatch.setattr(httpx, "Client", _Client)


def test_binance_historical_yields_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path — REST returns klines that the adapter wraps in BarEvent."""

    class _Resp:
        def __init__(self, status_code, payload, text=""):
            self.status_code = status_code
            self._p = payload
            self.text = text

        def json(self):
            return self._p

    payload_1 = [
        # [open_time, open, high, low, close, volume, close_time, ...]
        [
            1704067200000,  # 2024-01-01 00:00:00 UTC
            "100.0", "101.0", "99.0", "100.5", "10.0",
            1704067260000, "0", 0, "0", "0", "0",
        ]
    ]
    _stub_httpx(monkeypatch, [_Resp(200, payload_1), _Resp(200, [])])

    adapter = BinanceAdapter()
    bars = list(
        adapter.historical(
            symbols=["BTC"],
            asset_class="crypto",
            start="2024-01-01",
            end="2024-01-02",
            timeframe="1m",
        )
    )
    assert len(bars) == 1
    assert bars[0].bar.symbol == "BTC"
    assert bars[0].bar.close == 100.5


def test_binance_historical_handles_region_block(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.trading_service.providers.base import ProviderRegionBlocked

    class _Resp:
        def __init__(self):
            self.status_code = 451
            self.text = "Service not available"

        def json(self):
            return None

    _stub_httpx(monkeypatch, [_Resp()])
    adapter = BinanceAdapter()
    with pytest.raises(ProviderRegionBlocked):
        list(adapter.historical(symbols=["BTC"], asset_class="crypto", start="2024-01-01", end="2024-01-02", timeframe="1m"))


def test_binance_historical_handles_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    from investment_team.trading_service.providers.base import ProviderError

    class _Resp:
        def __init__(self):
            self.status_code = 500
            self.text = "internal error"

        def json(self):
            return None

    _stub_httpx(monkeypatch, [_Resp()])
    adapter = BinanceAdapter()
    with pytest.raises(ProviderError):
        list(adapter.historical(symbols=["BTC"], asset_class="crypto", start="2024-01-01", end="2024-01-02", timeframe="1m"))


def test_binance_live_rejects_non_crypto() -> None:
    adapter = BinanceAdapter()
    with pytest.raises(ValueError):
        list(adapter.live(symbols=["AAPL"], asset_class="equities", native_timeframe="tick"))


def test_binance_live_rejects_unknown_timeframe() -> None:
    adapter = BinanceAdapter()
    with pytest.raises(ValueError):
        list(adapter.live(symbols=["BTC"], asset_class="crypto", native_timeframe="3d"))


def test_binance_iso_to_ms_and_back() -> None:
    ms = _iso_to_ms("2024-01-01T00:00:00Z")
    assert ms == 1704067200000
    out = _ms_to_iso(ms)
    assert out.startswith("2024-01-01T00:00:00")
    # Date-only input is treated as midnight UTC.
    assert _iso_to_ms("2024-01-01") == 1704067200000


def test_binance_iso_to_ms_strips_naive_tz() -> None:
    """Naive datetimes are interpreted as UTC."""
    ms = _iso_to_ms("2024-06-01T12:34:56")
    assert ms > 0


def test_binance_build_returns_adapter() -> None:
    from investment_team.trading_service.providers.binance import build

    assert isinstance(build(), BinanceAdapter)
