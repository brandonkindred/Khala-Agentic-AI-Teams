"""Canonical symbol lists by asset class, shared across the investment team.

Used by the market data service (real OHLCV fetching), the deterministic backtest
engine (synthetic trade generation), and any future consumer that routes by asset class.
"""

from __future__ import annotations

from typing import Optional

# Yahoo Finance ticker mapping for crypto symbols (symbol → yfinance ticker)
YAHOO_CRYPTO_TICKERS: dict[str, str] = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "BNB": "BNB-USD",
    "XRP": "XRP-USD",
    "MATIC": "MATIC-USD",
    "AVAX": "AVAX-USD",
    "LINK": "LINK-USD",
    "ADA": "ADA-USD",
    "DOT": "DOT-USD",
}

# CoinGecko API ID mapping for crypto symbols (fallback provider)
COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "binancecoin",
    "XRP": "ripple",
    "MATIC": "matic-network",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "ADA": "cardano",
    "DOT": "polkadot",
}

STOCK_SYMBOLS: list[str] = [
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "AMZN",
    "META",
    "GOOGL",
    "JPM",
    "AMD",
    "SPY",
]
CRYPTO_SYMBOLS: list[str] = [
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "XRP",
    "MATIC",
    "AVAX",
    "LINK",
    "ADA",
    "DOT",
]
# Forex pairs use yfinance's =X suffix for real data; deterministic backtest uses bare names
FOREX_SYMBOLS: list[str] = [
    "EURUSD=X",
    "GBPUSD=X",
    "USDJPY=X",
    "AUDUSD=X",
    "USDCAD=X",
    "NZDUSD=X",
    "USDCHF=X",
    "EURGBP=X",
    "EURJPY=X",
    "GBPJPY=X",
]
# Bare forex names for the deterministic backtest engine (no =X suffix)
FOREX_SYMBOLS_BARE: list[str] = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
]
# Futures use yfinance's =F suffix
FUTURES_SYMBOLS: list[str] = ["ES=F", "NQ=F", "CL=F", "GC=F", "SI=F", "ZB=F", "NG=F"]
# Bare futures names for the deterministic backtest engine
FUTURES_SYMBOLS_BARE: list[str] = ["ES", "NQ", "CL", "GC", "SI", "ZB", "NG", "ZM", "ZS"]
# Commodity ETFs (liquid proxies for commodities via yfinance)
COMMODITY_SYMBOLS: list[str] = ["GLD", "USO", "SLV", "DBA", "UNG", "PDBC", "DBC"]
# Broad ETFs used as a fallback
OTHER_SYMBOLS: list[str] = ["GLD", "USO", "TLT", "QQQ", "IWM", "EEM", "GDX", "XLE", "XLF"]


def classify_symbol(symbol: str) -> Optional[str]:
    """Issue #523 — best-effort asset-class classification of a single ticker.

    Returns one of ``"stocks" | "crypto" | "forex" | "futures" | "commodities"``
    when the symbol unambiguously belongs to that class, else ``None``.

    Used to detect ``target_symbols`` vs ``asset_class`` mismatches (e.g.
    a strategy with ``asset_class="stocks"`` requesting ``target_symbols=["BTC"]``)
    so the operator gets a warning instead of a silent empty fetch.

    Cross-asset ETFs (``OTHER_SYMBOLS`` — GLD, USO, TLT, QQQ, ...) are
    deliberately *not* classified: they trade like stocks via Yahoo even
    when their underlying exposure is a different class, so flagging them
    would be a false positive in the common case.
    """
    sym = symbol.upper()

    if sym in OTHER_SYMBOLS:
        return None

    matched: list[str] = []
    if sym in STOCK_SYMBOLS:
        matched.append("stocks")
    if sym in CRYPTO_SYMBOLS:
        matched.append("crypto")
    if sym in FOREX_SYMBOLS or sym in FOREX_SYMBOLS_BARE:
        matched.append("forex")
    if sym in FUTURES_SYMBOLS or sym in FUTURES_SYMBOLS_BARE:
        matched.append("futures")
    if sym in COMMODITY_SYMBOLS:
        matched.append("commodities")

    if len(matched) == 1:
        return matched[0]
    if matched:
        return None  # ambiguous — don't guess

    # Suffix heuristics for symbols outside our canonical lists.
    if sym.endswith("=X"):
        return "forex"
    if sym.endswith("=F"):
        return "futures"
    if sym.endswith("-USD"):
        return "crypto"
    return None
