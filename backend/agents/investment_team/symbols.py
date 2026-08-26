"""Canonical symbol lists by asset class, shared across the investment team.

Used by the market data service (real OHLCV fetching), the deterministic backtest
engine (synthetic trade generation), and any future consumer that routes by asset class.

Universe-curation rationale
---------------------------
Each ``*_SYMBOLS`` list is the default universe used when ``spec.target_symbols``
is empty. The lists deliberately mix individual high-volume tickers (names that
recur in hypotheses — AAPL, NVDA, BTC, ES=F, ...) with index/sector ETFs (QQQ,
IWM, TLT, XLE, ...) so that theme-level hypotheses like "QQQ trend continuation"
or "energy sector rotation" do not silently fall through to a TSLA/AAPL universe
when ``target_symbols`` is omitted.

Cap interaction (contract with ``MarketDataService.resolve_strategy_symbols``):
the asset-class default is truncated to ``STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS``
(default ``20``) and is only used when ``spec.target_symbols`` is empty.
Non-empty ``spec.target_symbols`` is returned verbatim (override semantics)
so the fetched universe matches what ``TargetSymbolCoverageGate.check_trades``
allows the strategy to trade.

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

from typing import Iterable, Optional

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
    # Index / sector ETFs so theme-level hypotheses — "QQQ trend",
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

# ISO currency codes recognized by the bare-pair heuristic in classify_symbol,
# for a currency pair outside FOREX_SYMBOLS/FOREX_SYMBOLS_BARE (e.g. "USDNOK").
# Deliberately limited to codes that trade as forex majors/minors — a broader
# set would start colliding with six-letter tickers from other asset classes.
_FOREX_CURRENCY_CODES: frozenset[str] = frozenset(
    {
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "AUD",
        "CAD",
        "NZD",
        "CHF",
        "NOK",
        "SEK",
        "DKK",
        "MXN",
        "ZAR",
        "SGD",
        "HKD",
        "TRY",
    }
)


def asset_class_default_universe(asset_class: object) -> list[str]:
    """Default symbol universe for an asset class.

    Postcondition: returns the canonical default symbol list for the named
    class in declared order. Unknown / mistyped classes fall back to the
    stocks default to match the market data service's defensive default.
    Returns an empty list for ``"options"`` — the validator rejects that
    class upstream; the empty list is defense-in-depth for any path that
    skips the validator.

    The order returned here is the order the market data service preserves
    when capping the universe to ``STRATEGY_LAB_MAX_UNIVERSE_SYMBOLS``;
    callers wanting an unordered membership check should call ``set()`` on
    the result.

    Used by both ``MarketDataService.get_symbols_for_strategy`` (resolves
    the runtime fetch universe) and ``SpecReadinessGate._check_universe_set``
    (decides whether a hypothesis-named ticker is reachable without explicit
    ``target_symbols``). Centralising the mapping here is what keeps the
    gate's pass rule and the fetcher's resolved universe in lockstep.
    """
    # Local import to avoid a circular dependency at module-import time —
    # ``strategy_lab_context`` lives alongside ``symbols`` and is imported
    # by callers that also import from here.
    from .strategy_lab_context import normalize_asset_class

    asset = normalize_asset_class(asset_class)
    if asset == "crypto":
        return list(CRYPTO_SYMBOLS)
    if asset == "forex":
        return list(FOREX_SYMBOLS)
    if asset == "futures":
        return list(FUTURES_SYMBOLS)
    if asset == "commodities":
        return list(COMMODITY_SYMBOLS)
    if asset == "options":
        return []
    # "stocks" + defensive fallback for unknown classes.
    return list(STOCK_SYMBOLS)


def classify_symbol(symbol: str) -> Optional[str]:
    """Issue #523 — best-effort asset-class classification of a single ticker.

    Returns one of ``"stocks" | "crypto" | "forex" | "futures" | "commodities"``
    when the symbol is in the matching curated list, or matches a suffix/
    bare-pair heuristic for a symbol outside those lists, else ``None``. A
    curated-list match is certain; a heuristic match is best-effort and can
    in principle collide with an out-of-list ticker from another class (e.g.
    a hypothetical six-letter stock ticker that happens to spell two
    currency codes, caught by the bare-pair heuristic) — see the heuristic
    block below.

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
    # ``-USD`` is Yahoo's convention (see ``canonical_symbol``); ``-USDT``/
    # ``-USDC`` are the other quote-currency suffixes that convention
    # deliberately leaves unstripped, but they name crypto just as
    # unambiguously — a target symbol like ``DOGE-USDT`` must not be treated
    # as unclassified (and therefore silently allowed through an asset-class
    # pin check) just because it isn't in ``CRYPTO_SYMBOLS`` or Yahoo-suffixed.
    if sym.endswith(("-USD", "-USDT", "-USDC")):
        return "crypto"
    # Bare currency pair outside the curated lists (e.g. "USDNOK"): six
    # letters, both halves a recognized currency code, halves distinct.
    if len(sym) == 6 and sym.isalpha():
        base, quote = sym[:3], sym[3:]
        if base != quote and base in _FOREX_CURRENCY_CODES and quote in _FOREX_CURRENCY_CODES:
            return "forex"
    return None


def find_offcategory_symbols(symbols: Iterable[str], pinned_asset_class: str) -> set[str]:
    """The subset of ``symbols`` that :func:`classify_symbol` places in a different class.

    Single source of truth for the asset-category pin's symbol check, shared
    by the two places that independently need it from opposite ends: the
    quality gate that flags a mismatch as a readiness critical
    (``spec_readiness._check_asset_category_pin``) and the mechanical repair
    step that strips a minority mismatch (``mechanical_repair.repair_spec``).
    Keeping this predicate in one place means a future change to
    classification (a new suffix heuristic, a new cross-asset-ETF exemption)
    can never desynchronize what one flags from what the other strips.

    Preconditions:
      - ``symbols`` is an iterable of ticker strings.
      - ``pinned_asset_class`` is a canonical asset-class label.

    Postconditions:
      - Returns the set of symbols whose :func:`classify_symbol` result is
        not ``None`` and does not equal ``pinned_asset_class``. A symbol
        ``classify_symbol`` cannot unambiguously classify (a cross-asset ETF
        like ``GLD``/``QQQ``, or a genuinely unrecognized ticker) is never
        included — it is treated as allowed, not as off-category.
    """
    return {sym for sym in symbols if (cls := classify_symbol(sym)) is not None and cls != pinned_asset_class}


def canonical_symbol(symbol: str, asset_class: object) -> str:
    """Return the provider-neutral canonical form of a ticker.

    Crypto tickers travel through the system in two interchangeable spellings:
    the bare alias (``"BTC"``) and the Yahoo-suffixed form (``"BTC-USD"``). Each
    market-data provider re-suffixes the bare alias to its own convention
    (Yahoo → ``BTC-USD``, Twelve Data → ``BTC/USD``, Alpha Vantage → ``BTC``,
    CoinGecko → coin id), so the bare alias is the canonical internal form. This
    helper collapses both spellings to that single form so every provider — and
    the market-data cache key/fingerprint — sees one input per coin.

    Only the ``-USD`` suffix is stripped (it matches Yahoo's convention and the
    ``YAHOO_CRYPTO_TICKERS`` lookup keys). Other quote suffixes (``-USDT``,
    ``-USDC``) are intentionally left untouched. Stacked ``-USD`` suffixes are
    fully collapsed (``"DOGE-USD-USD" → "DOGE"``) so a compound-suffixed input
    can never re-emerge once a provider re-applies its own suffix.

    Preconditions:
        * ``symbol`` is a non-empty ticker string.
        * ``asset_class`` is any value accepted by ``normalize_asset_class``.

    Postconditions:
        * For crypto: returns ``symbol`` uppercased with *every* trailing
          ``-USD`` removed (``"btc-usd" → "BTC"``, ``"DOGE-USD-USD" → "DOGE"``,
          ``"BTC" → "BTC"``).
        * For every other asset class: returns ``symbol`` unchanged.
        * Idempotent — ``canonical_symbol(canonical_symbol(s, ac), ac) ==
          canonical_symbol(s, ac)``.

    Raises:
        * ``ValueError`` if a crypto ``symbol`` is nothing but ``-USD``
          suffix(es) and collapses to an empty alias (e.g. ``"-USD"``,
          ``"-USD-USD"``) — a precondition violation, surfaced rather than
          letting a malformed bare ticker reach a provider.
    """
    # Local import mirrors ``asset_class_default_universe`` — avoids a circular
    # dependency at module-import time.
    from .strategy_lab_context import normalize_asset_class

    if normalize_asset_class(asset_class) != "crypto":
        return symbol
    bare = symbol.upper()
    while bare.endswith("-USD"):
        bare = bare[: -len("-USD")]
    if not bare:
        raise ValueError(f"invalid crypto symbol: {symbol!r}")
    return bare
