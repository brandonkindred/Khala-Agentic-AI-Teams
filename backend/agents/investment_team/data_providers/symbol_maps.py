"""Per-provider ticker translation tables.

Each provider has a resolver function that converts a canonical symbol + asset class
into the provider-specific ticker string.  Falls back to identity (pass-through) when
no explicit mapping exists.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Twelve Data — https://twelvedata.com/docs
# ---------------------------------------------------------------------------

_TWELVE_DATA_FOREX: dict[str, str] = {
    "EURUSD=X": "EUR/USD",
    "GBPUSD=X": "GBP/USD",
    "USDJPY=X": "USD/JPY",
    "AUDUSD=X": "AUD/USD",
    "USDCAD=X": "USD/CAD",
    "NZDUSD=X": "NZD/USD",
    "USDCHF=X": "USD/CHF",
    "EURGBP=X": "EUR/GBP",
    "EURJPY=X": "EUR/JPY",
    "GBPJPY=X": "GBP/JPY",
}

_TWELVE_DATA_CRYPTO: dict[str, str] = {
    "BTC": "BTC/USD",
    "ETH": "ETH/USD",
    "SOL": "SOL/USD",
    "BNB": "BNB/USD",
    "XRP": "XRP/USD",
    "MATIC": "MATIC/USD",
    "AVAX": "AVAX/USD",
    "LINK": "LINK/USD",
    "ADA": "ADA/USD",
    "DOT": "DOT/USD",
}

_TWELVE_DATA_FUTURES: dict[str, str] = {
    "ES=F": "ES",
    "NQ=F": "NQ",
    "CL=F": "CL",
    "GC=F": "GC",
    "SI=F": "SI",
    "ZB=F": "ZB",
    "NG=F": "NG",
}


def resolve_twelve_data(symbol: str, asset_class: str) -> str:
    """Map a canonical symbol to Twelve Data ticker."""
    if asset_class == "crypto":
        return _TWELVE_DATA_CRYPTO.get(symbol.upper(), f"{symbol.upper()}/USD")
    if asset_class == "forex":
        return _TWELVE_DATA_FOREX.get(symbol, symbol.replace("=X", "").replace("=x", ""))
    if asset_class == "futures":
        return _TWELVE_DATA_FUTURES.get(symbol, symbol.replace("=F", "").replace("=f", ""))
    return symbol  # stocks, commodities, options — tickers match


# ---------------------------------------------------------------------------
# Alpha Vantage — https://www.alphavantage.co/documentation/
# ---------------------------------------------------------------------------

_AV_FOREX_PAIRS: dict[str, tuple[str, str]] = {
    "EURUSD=X": ("EUR", "USD"),
    "GBPUSD=X": ("GBP", "USD"),
    "USDJPY=X": ("USD", "JPY"),
    "AUDUSD=X": ("AUD", "USD"),
    "USDCAD=X": ("USD", "CAD"),
    "NZDUSD=X": ("NZD", "USD"),
    "USDCHF=X": ("USD", "CHF"),
    "EURGBP=X": ("EUR", "GBP"),
    "EURJPY=X": ("EUR", "JPY"),
    "GBPJPY=X": ("GBP", "JPY"),
}


def resolve_alphavantage_forex(symbol: str) -> tuple[str, str]:
    """Return (from_currency, to_currency) for Alpha Vantage FX_DAILY."""
    if symbol in _AV_FOREX_PAIRS:
        return _AV_FOREX_PAIRS[symbol]
    bare = symbol.replace("=X", "").replace("=x", "")
    return bare[:3], bare[3:6]


def resolve_alphavantage_stock(symbol: str) -> str:
    """Map a canonical stock/ETF/commodity symbol to Alpha Vantage ticker."""
    # Strip futures suffix — AV doesn't support futures directly but ETF proxies work
    return symbol.replace("=F", "").replace("=f", "")
