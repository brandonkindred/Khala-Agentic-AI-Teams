"""Realism-cycle quality gates.

This package collects the verification-phase gates that enforce
"the trade ledger resembles a real-world trading outcome":

* :mod:`.liquidity_realism` — every trade's position value must fit
  inside a realistic share of the symbol's average daily dollar volume.
* :mod:`.regime_coverage` — the strategy must show positive returns in
  every regime it traded in, and trade across more than one regime if
  the OOS window spans multiple.
* :mod:`.trade_clustering` — trade arrivals must be spread across the
  backtest window, not concentrated in a single quarter or fold.

Critical findings veto ``is_winning`` via the standard
:func:`_apply_veto_to_acceptance_reason` path in the orchestrator.
"""

from __future__ import annotations

from .liquidity_realism import LiquidityRealismGate
from .regime_coverage import RegimeCoverageGate
from .rule_firing import RuleFiringRateGate
from .trade_clustering import TradeClusteringGate

__all__ = [
    "LiquidityRealismGate",
    "RegimeCoverageGate",
    "RuleFiringRateGate",
    "TradeClusteringGate",
]
