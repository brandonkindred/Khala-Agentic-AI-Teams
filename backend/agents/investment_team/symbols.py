"""Canonical symbol lists by asset class, shared across the investment team.

Used by the market data service (real OHLCV fetching), the deterministic backtest
engine (synthetic trade generation), and any future consumer that routes by asset class.
"""

from __future__ import annotations

# CoinGecko API ID mapping for crypto symbols
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
