"""Coverage for ``data_providers.symbol_maps``."""

from __future__ import annotations

from investment_team.data_providers.symbol_maps import (
    resolve_alphavantage_forex,
    resolve_alphavantage_stock,
    resolve_twelve_data,
)


def test_resolve_twelve_data_crypto_known_and_unknown() -> None:
    assert resolve_twelve_data("BTC", "crypto") == "BTC/USD"
    assert resolve_twelve_data("DOGE", "crypto") == "DOGE/USD"


def test_resolve_twelve_data_forex_known_and_unknown() -> None:
    assert resolve_twelve_data("EURUSD=X", "forex") == "EUR/USD"
    # Unknown forex pair — strip ``=X`` suffix.
    assert resolve_twelve_data("XYZ=X", "forex") == "XYZ"


def test_resolve_twelve_data_futures_known_and_unknown() -> None:
    assert resolve_twelve_data("ES=F", "futures") == "ES"
    # Unknown futures contract — strip the suffix.
    assert resolve_twelve_data("XX=F", "futures") == "XX"


def test_resolve_twelve_data_passthrough_for_stocks() -> None:
    assert resolve_twelve_data("AAPL", "stocks") == "AAPL"


def test_resolve_alphavantage_forex_known_and_unknown() -> None:
    assert resolve_alphavantage_forex("EURUSD=X") == ("EUR", "USD")
    # Unknown pair — split first 3 / next 3 from the bare ticker.
    assert resolve_alphavantage_forex("CHFJPY=X") == ("CHF", "JPY")


def test_resolve_alphavantage_stock_strips_futures_suffix() -> None:
    assert resolve_alphavantage_stock("ES=F") == "ES"
    assert resolve_alphavantage_stock("AAPL") == "AAPL"


def test_resolve_alphavantage_stock_strips_lowercase_suffix() -> None:
    """The case-insensitive ``=F`` suffix stripping covers ``=f`` too."""
    assert resolve_alphavantage_stock("xx=f") == "xx"
