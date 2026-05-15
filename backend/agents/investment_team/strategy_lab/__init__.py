"""Strategy Lab v2 — code-generation + sandboxed execution backtesting pipeline."""

# Lazy re-export of StrategyLabOrchestrator. Importing it eagerly here pulls
# in `orchestrator` → `market_data_service` → `models`, which causes a
# circular import once `models.py` imports from `strategy_lab.spec_dsl`
# (issue #551). PEP 562 `__getattr__` keeps `from investment_team.strategy_lab
# import StrategyLabOrchestrator` working without forcing the chain at import
# time.

__all__ = ["StrategyLabOrchestrator"]


def __getattr__(name):
    if name == "StrategyLabOrchestrator":
        from .orchestrator import StrategyLabOrchestrator as _StrategyLabOrchestrator

        return _StrategyLabOrchestrator
    raise AttributeError(f"module 'investment_team.strategy_lab' has no attribute {name!r}")
