"""Execution primitives shared by backtesting and paper/live trading.

Modules here are deliberately free of LLM, HTTP, or persistence dependencies so
they can be reused by the future :class:`BacktestEngine` and :class:`LiveEngine`
(Phase 5) as well as the legacy :class:`TradeSimulationEngine` adapters.
"""

from .benchmarks import DEFAULT_BENCHMARK_BY_ASSET_CLASS, benchmark_for_strategy
from .metrics import (
    EquityCurve,
    PerformanceMetrics,
    build_equity_curve_from_trades,
    compute_performance_metrics,
)
from .risk_free_rate import get_risk_free_rate

__all__ = [
    "DEFAULT_BENCHMARK_BY_ASSET_CLASS",
    "EquityCurve",
    "PerformanceMetrics",
    "benchmark_for_strategy",
    "build_equity_curve_from_trades",
    "compute_performance_metrics",
    "get_risk_free_rate",
]
