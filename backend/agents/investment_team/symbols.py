"""Canonical symbol lists by asset class, shared across the investment team.

Used by the market data service (real OHLCV fetching), the deterministic backtest
engine (synthetic trade generation), and any future consumer that routes by asset class.

Universe-curation rationale (#534)
----------------------------------
Each ``*_SYMBOLS`` list is the default universe used when ``spec.target_symbols``
is empty. The lists deliberately mix individual high-volume tickers (names that
recur in LLM-generated hypotheses — AAPL, NVDA, BTC, ES=F, ...) with index/sector
ETFs (QQQ, IWM, TLT, XLE, ...) so that theme-level hypotheses like
"QQQ trend continuation" or "energy sector rotation" do not silently fall through
to a TSLA/AAPL universe when ``target_symbols`` is omitted.

Cap interaction: ``MarketDataService.resolve_strategy_symbols`` truncates the
asset-class default to ``STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS`` (default ``20``);
``spec.target_symbols`` is unioned on top of the (capped) default and is never
truncated (#525).

Adding new symbols:
  * Pick the most liquid representative for the theme (prefer SPY over a niche
    sector ETF; prefer ES=F over a thinly-traded contract).
  * Prefer ETFs over individual names when the theme is broad (QQQ over AMD for
    "semis", XLE over CVX for "energy").
  * Keep total list length ≤ the default cap so the full universe is reachable
    without an env-var override.
  * Cross-asset ETFs (broad/ambiguous exposure) belong in ``OTHER_SYMBOLS`` so
    ``classify_symbol`` doesn't false-positive on the mismatch warning.
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
    # Index / sector ETFs so theme-level hypotheses (#534) — "QQQ trend",
    # "small-cap rotation via IWM", "energy sector breakout via XLE" — don't
    # silently fall through to a TSLA/AAPL universe when target_symbols is
    # omitted. These also live in OTHER_SYMBOLS so classify_symbol still
    # treats them as ambiguous and the mismatch warning never false-positives.
    "QQQ",
    "IWM",
    "TLT",
    "EEM",
    "GDX",
    "XLE",
    "XLF",
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
