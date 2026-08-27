"""Lightweight market-regime summary for the DesignAgent prompt.

An expert swing trader picks the setup **to fit the regime**: momentum-
continuation pullbacks in trending / low-volatility markets, mean-reversion
fades in range-bound markets, volatility-contraction breakouts ahead of
expansion. The designer previously had no current-regime input, so it could
not condition its setup choice on the single biggest win-rate lever it has.

This module derives a compact, prompt-friendly regime summary from the same
market-data service the readiness / realism gates already use. For each
tracked asset-class benchmark it classifies:

  * **trend direction** — from the latest close vs its 50- and 200-period SMAs
  * **trend strength**   — bucketed from ADX(14)
  * **volatility regime** — the latest ATR(14)% ranked against its own trailing
    distribution (low / normal / high tercile)

Everything is fail-open: a data-fetch or compute error skips that benchmark and
marks the summary ``degraded`` rather than raising, so a market-data hiccup can
never crash a design cycle. Fetching is injected as a callable so tests stay
network-free (the same dependency-injection pattern used by the readiness
gate's ``market_sample_provider`` and the regime gate's ``vix_provider``).
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

import numpy as np
import pandas as pd
from pydantic import BaseModel, Field

from ..market_data_service import OHLCVBar
from ..strategy_lab_context import normalize_asset_class
from .executor.indicators import adx, atr, sma

logger = logging.getLogger(__name__)

# Callable that returns recent OHLCV bars for a (symbol, asset_class, days)
# request. Matches ``MarketDataService.fetch_ohlcv`` so the orchestrator can
# bind ``self.market_data_service.fetch_ohlcv`` directly while tests inject a
# network-free fake.
FetchOHLCV = Callable[[str, str, int], List[OHLCVBar]]

# Representative liquid benchmark per asset class the designer may choose. Kept
# small and extensible — one fetch per entry, cheap under the content-hashed
# market-data cache. Each pick matches the same class's default entry
# elsewhere in the codebase (``symbols.py``'s ``FOREX_SYMBOLS`` /
# ``FUTURES_SYMBOLS`` / ``COMMODITY_SYMBOLS`` all lead with the identical
# ticker), so the regime read and the backtest's own default universe agree
# on "the" representative instrument per class. All five classes are covered
# so a design attempt pinned to any of them still gets a ``## Market Regime``
# section — ``filter_regime_summary`` narrows the cross-class summary down to
# the pinned class alone, so an entry missing here means that pin's regime
# section renders blank, not merely unfiltered.
_DEFAULT_BENCHMARKS: dict[str, str] = {
    "stocks": "SPY",
    "crypto": "BTC-USD",
    "forex": "EURUSD=X",
    "futures": "ES=F",
    "commodities": "GLD",
}

# Lookback (calendar days requested) — enough to warm up a 200-period SMA and
# build a trailing ATR%-distribution with margin for missing/holiday bars.
_DEFAULT_DAYS = 400

# Minimum real bars needed to classify a benchmark. Below this the 200-SMA
# never warms up and the ATR distribution is too thin to rank against, so the
# entry is skipped rather than reported on weak evidence.
_MIN_BARS = 210


class RegimeEntry(BaseModel):
    """Per-asset-class regime classification.

    Invariants:
        ``trend_direction`` is one of ``up`` / ``down`` / ``sideways``;
        ``trend_strength`` is one of ``weak`` / ``moderate`` / ``strong``;
        ``volatility_regime`` is one of ``low`` / ``normal`` / ``high``.
        The raw numeric fields are the inputs the classification was derived
        from, exposed for observability / debugging.
    """

    asset_class: str
    benchmark_symbol: str
    trend_direction: str = Field(description="up | down | sideways")
    trend_strength: str = Field(description="weak | moderate | strong (from ADX)")
    volatility_regime: str = Field(description="low | normal | high (ATR% tercile)")
    close: float
    sma50: float
    sma200: float
    adx: float
    atr_pct: float
    atr_pct_percentile: float = Field(
        description="Rank in [0,1] of the latest ATR% within its trailing distribution"
    )


class RegimeSummary(BaseModel):
    """Compact, prompt-friendly market-regime snapshot across benchmarks.

    Invariants:
        ``entries`` holds one :class:`RegimeEntry` per successfully classified
        benchmark. When ``degraded`` is ``True`` at least one benchmark was
        skipped (or the whole computation failed) and ``degraded_reason`` gives
        a human-readable cause. An empty ``entries`` list with ``degraded`` is a
        valid "no regime available" result — callers treat it as absent.
    """

    computed_at: str
    degraded: bool = False
    degraded_reason: Optional[str] = None
    entries: List[RegimeEntry] = Field(default_factory=list)


def filter_regime_summary(
    summary: Optional[RegimeSummary], asset_class: Optional[str]
) -> Optional[RegimeSummary]:
    """Narrow a cross-asset regime summary to one asset class.

    The regime summary is computed once per cycle across every benchmark, so
    it renders one line per asset class into the design prompt. A design
    attempt pinned to a single category must not be shown four other markets'
    trend and volatility reads: they are irrelevant to its strategy and invite
    the designer to reason across categories it is forbidden to use.

    Preconditions:
        * ``summary`` is a :class:`RegimeSummary` or ``None``.
        * ``asset_class``, when given, is a canonical asset-class label.

    Postconditions:
        * Returns ``summary`` unchanged when either argument is ``None``.
        * Otherwise returns a new :class:`RegimeSummary` holding only the
          entries whose ``asset_class`` matches, preserving their order and
          the summary's ``computed_at``. The input is never mutated.
        * The returned summary always has ``degraded=False`` /
          ``degraded_reason=None``, regardless of the input's degraded state.
          ``degraded_reason`` names the specific benchmark ticker of
          whichever *other* categories failed to classify (e.g. ``"could not
          classify: ES=F (insufficient bars)"``) — carrying it through
          verbatim into a category-scoped prompt would leak a cross-category
          identifier straight past the pin's own "do not reference any other
          asset category" instruction. Reaching this branch already proves
          the pinned category's own benchmark classified successfully (a
          failed pinned benchmark means no matching entry, which returns
          ``None`` below instead), so the aggregate degraded state — which
          only ever describes *other*, now-stripped categories — no longer
          applies to this scoped view.
        * Returns ``None`` when no entry matches — an empty summary carries no
          information, and ``None`` is the shape every caller already treats
          as "no regime available", so this keeps the prompt's regime section
          absent rather than empty.
    """
    if summary is None or asset_class is None:
        return summary
    entries = [e for e in summary.entries if normalize_asset_class(e.asset_class) == asset_class]
    if not entries:
        return None
    return summary.model_copy(update={"entries": entries, "degraded": False, "degraded_reason": None})


def _classify_trend(close: float, sma50: float, sma200: float) -> str:
    """Classify trend direction from price relative to its moving averages.

    Pre: all three values are finite floats.
    Post: returns ``"up"`` when price and the fast MA lead the slow MA
    (close > sma50 > sma200), ``"down"`` in the mirror case, else
    ``"sideways"`` (mixed / crossing MAs — a range or transition).
    """
    if close > sma50 > sma200:
        return "up"
    if close < sma50 < sma200:
        return "down"
    return "sideways"


def _classify_trend_strength(adx_value: float) -> str:
    """Bucket ADX(14) into a strength label.

    Pre: ``adx_value`` is a finite float (ADX is bounded [0, 100]).
    Post: ``"weak"`` below 20 (no tradable trend), ``"moderate"`` in
    [20, 25), ``"strong"`` at 25+ — the conventional ADX thresholds.
    """
    if adx_value >= 25:
        return "strong"
    if adx_value >= 20:
        return "moderate"
    return "weak"


def _classify_volatility(atr_pct_percentile: float) -> str:
    """Bucket the latest ATR%'s trailing rank into a volatility regime.

    Pre: ``atr_pct_percentile`` is in [0, 1].
    Post: terciles — ``"low"`` below 1/3, ``"high"`` above 2/3, else
    ``"normal"``. Ranking against the benchmark's *own* history keeps the
    label meaningful across assets with different baseline volatility.
    """
    if atr_pct_percentile < 1 / 3:
        return "low"
    if atr_pct_percentile > 2 / 3:
        return "high"
    return "normal"


def _classify_benchmark(asset_class: str, symbol: str, bars: List[OHLCVBar]) -> RegimeEntry:
    """Compute a :class:`RegimeEntry` from a benchmark's OHLCV bars.

    Pre: ``bars`` is chronologically ordered and has at least ``_MIN_BARS``
    real bars (warms up the 200-SMA and ATR distribution).
    Post: returns a fully classified :class:`RegimeEntry`. Raises
    ``ValueError`` if the warmed-up indicators are non-finite (too few usable
    bars) — the caller treats that as a skipped benchmark.
    """
    assert len(bars) >= _MIN_BARS, "benchmark needs enough bars to warm up indicators"

    sma50 = float(sma(bars, 50).iloc[-1])
    sma200 = float(sma(bars, 200).iloc[-1])
    adx_value = float(adx(bars, bars, bars, period=14).iloc[-1])
    close = float(bars[-1].close)

    # ATR as a fraction of price, per bar, so the distribution is scale-free.
    # Dividing by a warmup-NaN ATR, a NaN close (gap/halt day), or a zero close
    # yields NaN/inf, all of which are dropped here — so no bad bar can leak
    # into the percentile. The latest bar's ATR is warmed up (``_MIN_BARS``)
    # and its close is finite (asserted below), so it survives as ``iloc[-1]``.
    atr_series = atr(bars, bars, bars, period=14).reset_index(drop=True)
    close_series = pd.Series([float(b.close) for b in bars], dtype=float)
    atr_pct = (atr_series / close_series).replace([np.inf, -np.inf], np.nan).dropna()
    # A perfectly flat benchmark has zero true range on every bar, so its ATR% is
    # all zeros — a degenerate series with no meaningful volatility distribution to
    # rank against. The indicator engine returns a finite 0.0 (not NaN) for such a
    # series, so this is treated as unclassifiable and skipped, preserving the
    # fail-open-skip contract that the earlier warmup-NaN guard provided.
    if (
        atr_pct.empty
        or (atr_pct == 0).all()
        or any(v != v for v in (sma50, sma200, adx_value, close))
    ):
        raise ValueError(f"{symbol}: indicators did not warm up on supplied bars")

    latest_atr_pct = float(atr_pct.iloc[-1])
    # Empirical CDF rank of the latest ATR% within its trailing distribution.
    atr_pct_percentile = float((atr_pct <= latest_atr_pct).mean())

    return RegimeEntry(
        asset_class=asset_class,
        benchmark_symbol=symbol,
        trend_direction=_classify_trend(close, sma50, sma200),
        trend_strength=_classify_trend_strength(adx_value),
        volatility_regime=_classify_volatility(atr_pct_percentile),
        close=close,
        sma50=sma50,
        sma200=sma200,
        adx=adx_value,
        atr_pct=latest_atr_pct,
        atr_pct_percentile=atr_pct_percentile,
    )


def compute_regime_summary(
    fetch_ohlcv: FetchOHLCV,
    *,
    computed_at: str,
    benchmarks: Optional[dict[str, str]] = None,
    days: int = _DEFAULT_DAYS,
) -> RegimeSummary:
    """Derive a :class:`RegimeSummary` across asset-class benchmarks.

    Fetching is injected via ``fetch_ohlcv`` so this stays network-free under
    test. ``computed_at`` is supplied by the caller (an ISO-UTC timestamp) —
    kept as a parameter rather than read from the clock so results are
    reproducible and the module has no wall-clock dependency.

    Pre: ``fetch_ohlcv`` is a callable ``(symbol, asset_class, days) ->
    List[OHLCVBar]``; ``computed_at`` is a non-empty string; ``days`` is a
    positive int.
    Post: returns a :class:`RegimeSummary`. Never raises — any per-benchmark
    fetch/compute failure skips that benchmark and sets ``degraded=True`` with
    a reason; a summary with no usable benchmarks is returned empty+degraded.
    Every returned :class:`RegimeEntry` was classified from real bars.

    Invariant: fail-open — a market-data outage degrades the summary, it never
    propagates an exception into the design cycle.
    """
    assert callable(fetch_ohlcv), "fetch_ohlcv must be callable"
    assert isinstance(computed_at, str) and computed_at, "computed_at must be a non-empty str"
    assert isinstance(days, int) and days > 0, "days must be a positive int"

    bench = benchmarks if benchmarks is not None else _DEFAULT_BENCHMARKS
    entries: List[RegimeEntry] = []
    skipped: List[str] = []

    for asset_class, symbol in bench.items():
        try:
            bars = fetch_ohlcv(symbol, asset_class, days)
            if not bars or len(bars) < _MIN_BARS:
                skipped.append(f"{symbol} (insufficient bars)")
                continue
            entries.append(_classify_benchmark(asset_class, symbol, bars))
        except Exception as exc:  # noqa: BLE001 — fail-open, degrade not crash
            logger.debug("regime classification failed for %s/%s: %s", symbol, asset_class, exc)
            skipped.append(f"{symbol} ({exc})")

    degraded = bool(skipped)
    reason = ("could not classify: " + "; ".join(skipped)) if skipped else None
    return RegimeSummary(
        computed_at=computed_at,
        degraded=degraded,
        degraded_reason=reason,
        entries=entries,
    )


def regime_to_prompt_block(summary: RegimeSummary) -> str:
    """Render a :class:`RegimeSummary` as a stable, compact prompt block.

    Pre: ``summary`` is a :class:`RegimeSummary`.
    Post: returns a newline-joined block — one line per classified benchmark
    plus a warning line when the summary is degraded. Never length-truncated so
    no decision-relevant line is dropped before reaching the prompt.
    """
    lines: List[str] = []
    for e in summary.entries:
        lines.append(
            f"- {e.asset_class} ({e.benchmark_symbol}): "
            f"trend={e.trend_direction} ({e.trend_strength}), "
            f"volatility={e.volatility_regime} "
            f"[ADX {e.adx:.0f}, ATR {e.atr_pct * 100:.1f}% at p{e.atr_pct_percentile * 100:.0f}]"
        )
    if summary.degraded and summary.degraded_reason:
        lines.append(f"NOTE: partial regime snapshot — {summary.degraded_reason}")
    if not lines:
        return "No current market-regime read available."
    return "\n".join(lines)
