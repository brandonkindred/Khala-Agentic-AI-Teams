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
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any, Dict, List, Optional

from .execution.metrics import EquityCurve
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
    risk_free_rate: Optional[float] = None,
    benchmark_equity: Optional[List[float]] = None,
    benchmark_dates: Optional[List[Any]] = None,
    equity_curve: Optional[EquityCurve] = None,
) -> BacktestResult:
    """Compute aggregate performance metrics from trade records.

    Uses the :mod:`investment_team.execution.metrics` daily-equity-curve
    estimator: proper Sharpe/Sortino/Calmar, max-DD duration, and risk-free
    rate from FRED (or ``STRATEGY_LAB_RISK_FREE_RATE`` env / ``RFR_DEFAULT``).

    ``equity_curve`` (#430) lets callers pass the streaming MTM curve from
    ``TradingService.run`` so the metrics engine can skip rebuilding one from
    the closed-trade ledger.
    """
    return _compute_metrics_daily(
        trades,
        initial_capital,
        start_date,
        end_date,
        risk_free_rate=risk_free_rate,
        benchmark_equity=benchmark_equity,
        benchmark_dates=benchmark_dates,
        equity_curve=equity_curve,
    )


def _compute_metrics_daily(
    trades: List[TradeRecord],
    initial_capital: float,
    start_date: str,
    end_date: str,
    *,
    risk_free_rate: Optional[float] = None,
    benchmark_equity: Optional[List[float]] = None,
    benchmark_dates: Optional[List[Any]] = None,
    equity_curve: Optional[EquityCurve] = None,
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
        equity_curve=equity_curve,
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
    )
