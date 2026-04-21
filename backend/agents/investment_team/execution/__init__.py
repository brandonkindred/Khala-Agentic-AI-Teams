"""Execution primitives shared by backtesting and paper/live trading.

Modules here are deliberately free of LLM, HTTP, or persistence dependencies so
they can be reused by the future :class:`BacktestEngine` and :class:`LiveEngine`
(Phase 5) as well as the legacy :class:`TradeSimulationEngine` adapters.
"""

from .benchmarks import (
    DEFAULT_BENCHMARK_BY_ASSET_CLASS,
    benchmark_for_strategy,
    build_60_40_equity,
    build_weighted_blend_equity,
)
from .cost_model import (
    CostModel,
    FlatBpsCostModel,
    MakerTakerCostModel,
    SpreadPlusImpactCostModel,
    build_cost_model,
)
from .metrics import (
    EquityCurve,
    PerformanceMetrics,
    bootstrap_sharpe_ci,
    build_equity_curve_from_trades,
    compute_deflated_sharpe,
    compute_performance_metrics,
    summarize_return_moments,
)
from .regimes import (
    REGIME_LABELS,
    realized_vol_quartile_subwindows,
    regime_comparison,
    vix_quartile_subwindows,
)
from .risk_filter import RiskFilter, RiskLimits
from .risk_free_rate import get_risk_free_rate
from .walk_forward import (
    DateRange,
    Fold,
    build_purged_walk_forward,
    filter_trades_in_fold_training,
    filter_trades_in_range,
    max_hold_days_from_trades,
)

__all__ = [
    "CostModel",
    "DEFAULT_BENCHMARK_BY_ASSET_CLASS",
    "DateRange",
    "EquityCurve",
    "FlatBpsCostModel",
    "Fold",
    "MakerTakerCostModel",
    "PerformanceMetrics",
    "REGIME_LABELS",
    "RiskFilter",
    "RiskLimits",
    "SpreadPlusImpactCostModel",
    "benchmark_for_strategy",
    "bootstrap_sharpe_ci",
    "build_60_40_equity",
    "build_cost_model",
    "build_equity_curve_from_trades",
    "build_purged_walk_forward",
    "build_weighted_blend_equity",
    "compute_deflated_sharpe",
    "compute_performance_metrics",
    "filter_trades_in_fold_training",
    "filter_trades_in_range",
    "get_risk_free_rate",
    "max_hold_days_from_trades",
    "realized_vol_quartile_subwindows",
    "regime_comparison",
    "summarize_return_moments",
    "vix_quartile_subwindows",
]
