"""Quality gates for Strategy Lab: validation, code safety, anomaly detection, convergence."""

from .acceptance_gate import AcceptanceGate, summarize_acceptance_reason
from .backtest_anomaly import BacktestAnomalyDetector
from .code_safety import CodeSafetyChecker
from .convergence_tracker import ConvergenceTracker
from .models import QualityGateResult, StrategyLabPhase
from .predicate_conformance import PredicateConformanceGate
from .spec_readiness import MAX_POSITION_PCT_CEILING, SpecReadinessGate, extract_known_tickers
from .strategy_validator import StrategySpecValidator
from .target_symbol_coverage import TargetSymbolCoverageGate

__all__ = [
    "AcceptanceGate",
    "BacktestAnomalyDetector",
    "CodeSafetyChecker",
    "ConvergenceTracker",
    "MAX_POSITION_PCT_CEILING",
    "PredicateConformanceGate",
    "QualityGateResult",
    "SpecReadinessGate",
    "StrategyLabPhase",
    "StrategySpecValidator",
    "TargetSymbolCoverageGate",
    "extract_known_tickers",
    "summarize_acceptance_reason",
]
