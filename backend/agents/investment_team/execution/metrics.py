"""Daily-equity-curve performance metrics.

Replaces the legacy inter-trade-return Sharpe estimator in
:func:`investment_team.trade_simulator.compute_metrics`. The old estimator
treated each trade exit as one "period" return and annualized by the average
gap between exits — mathematically wrong when holding periods vary, and it
produced absurd Sharpe values (>100) on the golden fixtures.

The new pipeline:

1. Mark every open position to ``bar.close`` each trading day.
2. Carry cash through weekends/holidays (equity is flat on non-trading days).
3. Compute daily log returns → annualize with ``sqrt(252)`` (or the closest
   integer match to the dataset when only weekdays are present).
4. Report Sharpe, Sortino, Calmar, max drawdown + duration, CAGR, hit rate,
   profit factor, and alpha/beta vs. a benchmark series when supplied.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING, Iterable, List, Optional, Sequence

from .risk_free_rate import get_risk_free_rate

if TYPE_CHECKING:  # pragma: no cover — avoids models ↔ execution import cycle
    from ..models import TradeRecord

TRADING_DAYS_PER_YEAR = 252


@dataclass
class EquityCurve:
    """A daily mark-to-market equity curve anchored on an initial capital.

    ``dates`` and ``equity`` are aligned 1-to-1 and chronologically sorted.
    ``equity[i]`` is the EOD portfolio value on ``dates[i]``.
    """

    dates: List[date] = field(default_factory=list)
    equity: List[float] = field(default_factory=list)
    initial_capital: float = 0.0

    def daily_returns(self) -> List[float]:
        """Simple arithmetic daily returns from the equity series."""
        if len(self.equity) < 2:
            return []
        out: List[float] = []
        for i in range(1, len(self.equity)):
            prev = self.equity[i - 1]
            cur = self.equity[i]
            if prev <= 0:
                out.append(0.0)
            else:
                out.append((cur - prev) / prev)
        return out


@dataclass
class PerformanceMetrics:
    total_return_pct: float
    annualized_return_pct: float
    volatility_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    max_drawdown_duration_days: int
    win_rate_pct: float
    profit_factor: float
    risk_free_rate: float
    trade_count: int
    alpha_pct: Optional[float] = None
    beta: Optional[float] = None
    information_ratio: Optional[float] = None


# ---------------------------------------------------------------------------
# Equity curve construction
# ---------------------------------------------------------------------------


def _parse_date(s: str) -> date:
    return date.fromisoformat(s[:10])


def _weekday_range(start: date, end: date) -> List[date]:
    out: List[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_equity_curve_from_trades(
    trades: Sequence[TradeRecord],
    initial_capital: float,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> EquityCurve:
    """Construct a daily equity curve from a completed trade ledger.

    When only closed trades are available (today's ``TradeRecord`` list), the
    curve is approximated as piecewise-constant between exit dates: equity
    steps up/down by ``net_pnl`` on each trade's ``exit_date`` and is flat on
    all other trading days. This is strictly more accurate than the legacy
    inter-trade-return estimator for risk metrics, and it's identical in the
    limit when MTM on open positions is added by the Phase 5 engine.
    """
    if not trades and start_date is None:
        return EquityCurve(dates=[], equity=[], initial_capital=initial_capital)

    all_trade_dates: List[date] = []
    for t in trades:
        all_trade_dates.append(_parse_date(t.entry_date))
        all_trade_dates.append(_parse_date(t.exit_date))

    if start_date:
        span_start = _parse_date(start_date)
    else:
        span_start = min(all_trade_dates) if all_trade_dates else date.today()
    if end_date:
        span_end = _parse_date(end_date)
    else:
        span_end = max(all_trade_dates) if all_trade_dates else span_start

    if span_end < span_start:
        span_end = span_start

    dates = _weekday_range(span_start, span_end)
    if not dates:
        return EquityCurve(dates=[], equity=[], initial_capital=initial_capital)

    pnl_by_exit_date: dict[date, float] = {}
    for t in trades:
        d = _parse_date(t.exit_date)
        pnl_by_exit_date[d] = pnl_by_exit_date.get(d, 0.0) + t.net_pnl

    equity: List[float] = []
    running = initial_capital
    for d in dates:
        running = round(running + pnl_by_exit_date.get(d, 0.0), 4)
        equity.append(running)

    return EquityCurve(dates=dates, equity=equity, initial_capital=initial_capital)


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def _std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _max_drawdown(equity: Sequence[float]) -> tuple[float, int]:
    if not equity:
        return 0.0, 0
    peak = equity[0]
    peak_idx = 0
    max_dd = 0.0
    max_dur = 0
    for i, v in enumerate(equity):
        if v > peak:
            peak = v
            peak_idx = i
        if peak > 0:
            dd = (peak - v) / peak
            if dd > max_dd:
                max_dd = dd
                max_dur = i - peak_idx
    return max_dd, max_dur


def _alpha_beta(
    portfolio_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    risk_free_daily: float,
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return ``(alpha_pct_annualized, beta, information_ratio)``.

    Regression: ``(Rp - Rf) = alpha + beta * (Rb - Rf) + eps`` on daily data.
    """
    n = min(len(portfolio_returns), len(benchmark_returns))
    if n < 10:
        return None, None, None

    pr = [portfolio_returns[i] - risk_free_daily for i in range(n)]
    br = [benchmark_returns[i] - risk_free_daily for i in range(n)]

    mean_b = sum(br) / n
    mean_p = sum(pr) / n
    cov = sum((br[i] - mean_b) * (pr[i] - mean_p) for i in range(n)) / (n - 1)
    var_b = sum((x - mean_b) ** 2 for x in br) / (n - 1)

    if var_b <= 0:
        return None, None, None

    beta = cov / var_b
    alpha_daily = mean_p - beta * mean_b
    alpha_annual_pct = ((1 + alpha_daily) ** TRADING_DAYS_PER_YEAR - 1) * 100

    active = [portfolio_returns[i] - benchmark_returns[i] for i in range(n)]
    tracking = _std(active)
    ir = (sum(active) / n) / tracking * math.sqrt(TRADING_DAYS_PER_YEAR) if tracking > 0 else None

    return round(alpha_annual_pct, 3), round(beta, 4), (round(ir, 3) if ir is not None else None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_performance_metrics(
    trades: Sequence[TradeRecord],
    initial_capital: float,
    *,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    risk_free_rate: Optional[float] = None,
    benchmark_equity: Optional[Sequence[float]] = None,
    benchmark_dates: Optional[Iterable[date]] = None,
) -> PerformanceMetrics:
    """Compute daily-equity-curve performance metrics.

    ``benchmark_equity`` (aligned to ``benchmark_dates``) is optional; when
    present, alpha/beta/information-ratio are filled in.
    """
    rfr = get_risk_free_rate(override=risk_free_rate)

    if not trades:
        return PerformanceMetrics(
            total_return_pct=0.0,
            annualized_return_pct=0.0,
            volatility_pct=0.0,
            sharpe_ratio=0.0,
            sortino_ratio=0.0,
            calmar_ratio=0.0,
            max_drawdown_pct=0.0,
            max_drawdown_duration_days=0,
            win_rate_pct=0.0,
            profit_factor=0.0,
            risk_free_rate=round(rfr, 6),
            trade_count=0,
        )

    curve = build_equity_curve_from_trades(
        trades, initial_capital, start_date=start_date, end_date=end_date
    )
    returns = curve.daily_returns()

    total_pnl = sum(t.net_pnl for t in trades)
    total_return_frac = total_pnl / initial_capital if initial_capital > 0 else 0.0
    total_return_pct = round(total_return_frac * 100, 3)

    # CAGR from the equity span.
    if curve.dates:
        span_days = max(1, (curve.dates[-1] - curve.dates[0]).days)
        years = max(span_days / 365.25, 1 / 365.25)
        if 1 + total_return_frac > 0:
            annualized_return_frac = (1 + total_return_frac) ** (1 / years) - 1
        else:
            annualized_return_frac = -1.0
    else:
        annualized_return_frac = 0.0
    annualized_return_pct = round(annualized_return_frac * 100, 3)

    # Daily vol → annualized.
    daily_vol = _std(returns)
    annualized_vol_frac = daily_vol * math.sqrt(TRADING_DAYS_PER_YEAR)
    annualized_vol_pct = round(annualized_vol_frac * 100, 3)

    # Sharpe (excess-return / vol, annualized).
    if annualized_vol_frac > 0:
        sharpe = (annualized_return_frac - rfr) / annualized_vol_frac
    else:
        sharpe = 0.0

    # Sortino (downside deviation).
    downside = [r for r in returns if r < 0]
    dd_vol = _std(downside) * math.sqrt(TRADING_DAYS_PER_YEAR) if downside else 0.0
    sortino = (annualized_return_frac - rfr) / dd_vol if dd_vol > 0 else 0.0

    # Max drawdown & duration.
    max_dd_frac, max_dd_days = _max_drawdown(curve.equity)
    max_dd_pct = round(max_dd_frac * 100, 3)

    # Calmar = annual_return / max_drawdown.
    calmar = (annualized_return_frac / max_dd_frac) if max_dd_frac > 0 else 0.0

    # Trade-level stats.
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    win_rate = round(len(wins) / len(trades) * 100, 3) if trades else 0.0
    gross_wins = sum(t.net_pnl for t in wins)
    gross_losses = -sum(t.net_pnl for t in losses)
    if gross_losses > 0:
        profit_factor = round(gross_wins / gross_losses, 3)
    else:
        profit_factor = round(gross_wins, 3)

    alpha_pct = beta = info_ratio = None
    if benchmark_equity and benchmark_dates is not None:
        bench_returns = _align_benchmark_returns(
            curve, list(benchmark_dates), list(benchmark_equity)
        )
        if bench_returns is not None:
            alpha_pct, beta, info_ratio = _alpha_beta(
                returns, bench_returns, risk_free_daily=rfr / TRADING_DAYS_PER_YEAR
            )

    return PerformanceMetrics(
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return_pct,
        volatility_pct=annualized_vol_pct,
        sharpe_ratio=round(sharpe, 4),
        sortino_ratio=round(sortino, 4),
        calmar_ratio=round(calmar, 4),
        max_drawdown_pct=max_dd_pct,
        max_drawdown_duration_days=int(max_dd_days),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
        risk_free_rate=round(rfr, 6),
        trade_count=len(trades),
        alpha_pct=alpha_pct,
        beta=beta,
        information_ratio=info_ratio,
    )


def _align_benchmark_returns(
    curve: EquityCurve,
    bench_dates: List[date],
    bench_equity: List[float],
) -> Optional[List[float]]:
    if len(bench_dates) != len(bench_equity) or len(curve.dates) < 2:
        return None
    by_date = dict(zip(bench_dates, bench_equity))
    aligned: List[float] = []
    for d in curve.dates:
        val = by_date.get(d)
        if val is None:
            return None
        aligned.append(val)
    returns: List[float] = []
    for i in range(1, len(aligned)):
        prev = aligned[i - 1]
        if prev <= 0:
            returns.append(0.0)
        else:
            returns.append((aligned[i] - prev) / prev)
    return returns
