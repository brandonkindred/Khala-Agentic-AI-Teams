"""Investment team domain exceptions.

Kept in a standalone module so service-layer helpers (e.g. the real-data
backtest pipeline) can raise business-rule failures without depending on
``fastapi``, keeping them usable from non-HTTP callers such as Temporal
activities, CLI tools, and unit tests. HTTP-facing callers translate these
into the appropriate ``HTTPException``.
"""

from __future__ import annotations


class InvestmentBacktestError(Exception):
    """Base for domain-level failures raised by the real-data backtest pipeline.

    Preconditions:
        Raised only by ``_run_real_data_backtest`` and its callees.
    Postconditions:
        ``str(exc)`` carries a human-readable failure message suitable for
        direct display to a caller (job error string, HTTP detail, etc.).
    """


class MissingStrategyCodeError(InvestmentBacktestError):
    """Raised when a ``StrategySpec`` has no generated ``strategy_code`` to execute."""


class MarketDataUnavailableError(InvestmentBacktestError):
    """Raised when the market data service returns no bars for the requested symbols/range."""


class StrategyExecutionError(InvestmentBacktestError):
    """Raised when a generated strategy script fails during backtest execution."""


class LookaheadViolationError(StrategyExecutionError):
    """Raised when a generated strategy script accesses look-ahead (future) market data.

    Subclasses ``StrategyExecutionError`` since both are execution-time
    failures of the generated script — callers that only care about "did
    execution fail" can catch the parent; callers that need to distinguish a
    look-ahead violation from any other execution error can catch this
    subclass specifically.
    """
