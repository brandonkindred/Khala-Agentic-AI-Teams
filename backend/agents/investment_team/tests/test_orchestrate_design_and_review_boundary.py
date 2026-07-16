"""Coverage for the DESIGN_REVIEW -> CODE_SYNTHESIS boundary invariant in
``StrategyLabOrchestrator._orchestrate_design_and_review``.

The invariant check was previously a bare ``assert``, silently disabled
under ``python -O``. It is now an explicit ``if``/``raise`` guard that must
always fire, regardless of interpreter optimization flags.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from investment_team.models import BacktestConfig
from investment_team.strategy_lab.orchestrator import (
    StrategyLabOrchestrator,
    _DesignLoopOutcome,
    _DriftCollector,
)
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate, SignalExitRule


def _spec_dict() -> Dict[str, Any]:
    return {
        "asset_class": "stocks",
        "hypothesis": "RSI mean reversion on a small universe",
        "signal_definition": "RSI(14) crossings",
        "timeframe": "1d",
        "entry_rules": [
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op="<",
                    rhs=30,
                ),
            ).model_dump()
        ],
        "exit_rules": [
            SignalExitRule(
                when=Predicate(
                    lhs=IndicatorRef(name="rsi", params={"period": 14}),
                    op=">",
                    rhs=70,
                )
            ).model_dump()
        ],
        "risk_limits": {"max_position_pct": 5, "max_drawdown_pct": 10},
        "target_symbols": ["QQQ"],
        "speculative": False,
    }


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
    )


class _ReadyFlipsToNotReadyOutcome:
    """Proxy over a real ``_DesignLoopOutcome`` whose ``ready`` reads ``True``
    once, then ``False`` forever after.

    ``_orchestrate_design_and_review`` reads ``design_outcome.ready`` exactly
    twice: once to decide whether to take the short-circuit (not-ready)
    branch, and once at the boundary-invariant guard just before emitting the
    DESIGN_REVIEW -> CODE_SYNTHESIS transition. A plain bool can't simulate
    the invariant being violated between those two reads — this proxy can.
    """

    def __init__(self, base: _DesignLoopOutcome) -> None:
        self._base = base
        self._reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)

    @property
    def ready(self) -> bool:
        self._reads += 1
        return self._reads == 1


def test_orchestrate_design_and_review_raises_when_ready_flips_false() -> None:
    """If ``design_outcome.ready`` is no longer true by the time the
    boundary guard runs, the guard must raise ``RuntimeError`` — not
    silently proceed to emit the phase transition and return a
    ``record=None`` result, which is what a stripped-under--O bare
    ``assert`` would have allowed."""
    orch = StrategyLabOrchestrator()
    spec = orch._build_spec_from_dict(_spec_dict(), strategy_id="strat-boundary-test")
    base_outcome = _DesignLoopOutcome(
        spec=spec,
        rationale="scripted rationale",
        ready=True,
        rounds=1,
        critique_history=[],
    )
    flaky_outcome = _ReadyFlipsToNotReadyOutcome(base_outcome)

    orch._run_design_loop = lambda **_kw: flaky_outcome

    with pytest.raises(RuntimeError, match="boundary invariant violated"):
        orch._orchestrate_design_and_review(
            prior_records=[],
            signal_brief=None,
            directives=[],
            exclude_asset_classes=None,
            config=_config(),
            all_gate_results=[],
            emit=lambda *a, **k: None,
            design_attempt=0,
            phase_back_count=0,
            drift_collector=_DriftCollector(),
        )
