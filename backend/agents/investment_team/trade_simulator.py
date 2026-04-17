"""Metric computation + shared position/result dataclasses.

PR 3 retired ``TradeSimulationEngine`` and the bar-by-bar evaluation loop
that lived here — strategy code now runs through the event-driven
``trading_service`` package exclusively. What remains is the stable set of
helpers downstream consumers depend on:

* :func:`compute_metrics` — canonical P&L / Sharpe / DD estimator used by
  both ``run_backtest`` and ``PaperTradingAgent``.
* :func:`date_diff_days` — calendar-day diff used in a handful of tests
  and metric paths.
* :class:`OpenPosition` — compact dataclass still used by
  ``RiskFilter.can_enter`` unit tests to pass position snapshots.
* :class:`SimulationResult` — preserved for legacy consumers; kept unused
  by the current engine.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any, Dict, List, Optional

from .models import BacktestResult, TradeRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------


@dataclass
class OpenPosition:
    """Typed container for an open trading position.

    ``entry_price`` is the fill price (post-slippage). ``entry_bid_price`` is
    the raw reference close used as the baseline before slippage was applied,
    captured so the eventual ``TradeRecord`` can expose both. ``entry_order_type``
    defaults to ``"market"`` (every simulated fill is at bar close); the field
    is here so future limit/stop-order simulation can set a different value
    without touching downstream code.
    """

    symbol: str
    side: str  # "long" or "short"
    entry_date: str
    entry_price: float
    shares: float
    position_value: float
    entry_bid_price: float = 0.0
    entry_order_type: str = "market"


# ---------------------------------------------------------------------------
# Simulation result
# ---------------------------------------------------------------------------


@dataclass
class SimulationResult:
    """Container for results from a trade simulation run."""

    trades: List[TradeRecord] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    final_capital: float = 0.0
    forced_close_count: int = 0
    evaluations_performed: int = 0
    bars_skipped_by_filter: int = 0
    terminated_reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def date_diff_days(d1: str, d2: str) -> int:
    """Compute days between two ISO date strings. Returns at least 1."""
    try:
        dt1 = date_cls.fromisoformat(d1[:10])
        dt2 = date_cls.fromisoformat(d2[:10])
        return max(1, abs((dt2 - dt1).days))
    except (ValueError, TypeError):
        return 1


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def compute_metrics(
    trades: List[TradeRecord],
    initial_capital: float,
    start_date: str,
    end_date: str,
    *,
    metrics_engine: str = "daily",
    risk_free_rate: Optional[float] = None,
    benchmark_equity: Optional[List[float]] = None,
    benchmark_dates: Optional[List[Any]] = None,
) -> BacktestResult:
    """Compute aggregate performance metrics from trade records.

    ``metrics_engine="daily"`` (default, Phase 1) uses the
    :mod:`investment_team.execution.metrics` daily-equity-curve estimator:
    proper Sharpe/Sortino/Calmar, max-DD duration, and risk-free rate from
    FRED (or the ``STRATEGY_LAB_RISK_FREE_RATE`` env / ``RFR_DEFAULT``).

    ``metrics_engine="legacy"`` preserves the pre-refactor inter-trade-return
    estimator for one release so persisted results stay byte-identical when
    explicitly requested.
    """
    if metrics_engine == "daily":
        return _compute_metrics_daily(
            trades,
            initial_capital,
            start_date,
            end_date,
            risk_free_rate=risk_free_rate,
            benchmark_equity=benchmark_equity,
            benchmark_dates=benchmark_dates,
        )
    return _compute_metrics_legacy(trades, initial_capital, start_date, end_date)


def _compute_metrics_daily(
    trades: List[TradeRecord],
    initial_capital: float,
    start_date: str,
    end_date: str,
    *,
    risk_free_rate: Optional[float] = None,
    benchmark_equity: Optional[List[float]] = None,
    benchmark_dates: Optional[List[Any]] = None,
) -> BacktestResult:
    from .execution.metrics import compute_performance_metrics

    m = compute_performance_metrics(
        trades,
        initial_capital,
        start_date=start_date or None,
        end_date=end_date or None,
        risk_free_rate=risk_free_rate,
        benchmark_equity=benchmark_equity,
        benchmark_dates=benchmark_dates,
    )
    return BacktestResult(
        total_return_pct=round(m.total_return_pct, 2),
        annualized_return_pct=round(m.annualized_return_pct, 2),
        volatility_pct=round(m.volatility_pct, 2),
        sharpe_ratio=round(m.sharpe_ratio, 2),
        max_drawdown_pct=round(m.max_drawdown_pct, 2),
        win_rate_pct=round(m.win_rate_pct, 2),
        profit_factor=m.profit_factor,
        sortino_ratio=round(m.sortino_ratio, 2),
        calmar_ratio=round(m.calmar_ratio, 2),
        max_drawdown_duration_days=m.max_drawdown_duration_days,
        risk_free_rate=m.risk_free_rate,
        alpha_pct=m.alpha_pct,
        beta=m.beta,
        information_ratio=m.information_ratio,
        metrics_engine="daily",
    )


def _compute_metrics_legacy(
    trades: List[TradeRecord],
    initial_capital: float,
    start_date: str,
    end_date: str,
) -> BacktestResult:
    """Pre-Phase-1 inter-trade-return estimator. Retained for diff runs."""
    if not trades:
        return BacktestResult(
            total_return_pct=0.0,
            annualized_return_pct=0.0,
            volatility_pct=0.0,
            sharpe_ratio=0.0,
            max_drawdown_pct=0.0,
            win_rate_pct=0.0,
            profit_factor=0.0,
            metrics_engine="legacy",
        )

    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]

    # Total return
    total_pnl = sum(t.net_pnl for t in trades)
    total_return_frac = total_pnl / initial_capital
    total_return_pct = round(total_return_frac * 100, 2)

    # CAGR-based annualized return
    total_days = date_diff_days(start_date, end_date)
    years = max(total_days / 365.25, 0.01)
    if total_return_frac > -1.0:
        annualized_return = round(((1 + total_return_frac) ** (1 / years) - 1) * 100, 2)
    else:
        annualized_return = -100.0

    # Win rate
    win_rate = round(len(wins) / len(trades) * 100, 2)

    # Profit factor
    gross_wins = sum(t.gross_pnl for t in wins) if wins else 0.0
    gross_losses = abs(sum(t.gross_pnl for t in losses)) if losses else 0.0
    profit_factor = (
        round(gross_wins / gross_losses, 2) if gross_losses > 0 else round(max(gross_wins, 0.0), 2)
    )

    # Volatility from equity curve at trade exits.
    # Compute returns between consecutive trade exits and annualize by the
    # average gap between exits, avoiding the incorrect assumption that
    # per-trade returns are daily returns.
    exit_equities: List[tuple[str, float]] = []
    equity = initial_capital
    for t in trades:
        equity += t.net_pnl
        exit_equities.append((t.exit_date, equity))

    annualized_vol = 0.0
    if len(exit_equities) > 1:
        inter_trade_returns: List[float] = []
        prev_eq = initial_capital
        for _, eq in exit_equities:
            if prev_eq > 0:
                inter_trade_returns.append((eq - prev_eq) / prev_eq)
            prev_eq = eq

        if len(inter_trade_returns) > 1:
            mean_r = sum(inter_trade_returns) / len(inter_trade_returns)
            var = sum((r - mean_r) ** 2 for r in inter_trade_returns) / (
                len(inter_trade_returns) - 1
            )
            total_span = date_diff_days(exit_equities[0][0], exit_equities[-1][0])
            avg_gap = max(total_span / len(inter_trade_returns), 1)
            periods_per_year = 365.25 / avg_gap
            annualized_vol = round(math.sqrt(var * periods_per_year), 2)

    # Sharpe ratio
    sharpe = round(annualized_return / annualized_vol, 2) if annualized_vol > 0 else 0.0

    # Max drawdown
    peak = initial_capital
    max_dd = 0.0
    equity = initial_capital
    for t in trades:
        equity += t.net_pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return BacktestResult(
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return,
        volatility_pct=annualized_vol,
        sharpe_ratio=sharpe,
        max_drawdown_pct=round(max_dd, 2),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        metrics_engine="legacy",
    )
