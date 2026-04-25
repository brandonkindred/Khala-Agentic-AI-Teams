"""Regression for ``BacktestAnomalyDetector(dsr_aware=...)`` (issue #247).

When walk-forward + ``AcceptanceGate`` is wired into the orchestrator (step 8
of issue #247), the OOS Deflated Sharpe is the authoritative overfitting
check, so the legacy ``Sharpe > 5.0`` single-window flag must downgrade from
critical to warning to avoid double-rejecting the same strategy. The flag is
preserved (it still surfaces in the gate result list and feeds into the
gate-history persistence) but it no longer forces a refinement-loop rewrite.
"""

from __future__ import annotations

from typing import List

from investment_team.models import BacktestResult, TradeRecord
from investment_team.strategy_lab.quality_gates.backtest_anomaly import (
    BacktestAnomalyDetector,
)


def _trades(n: int = 12) -> List[TradeRecord]:
    out: List[TradeRecord] = []
    cum = 0.0
    for i in range(n):
        net = 25.0 if i % 2 == 0 else -10.0
        cum += net
        out.append(
            TradeRecord(
                trade_num=i + 1,
                entry_date=f"2023-01-{(i % 28) + 1:02d}",
                exit_date=f"2023-02-{(i % 28) + 1:02d}",
                symbol="AAPL" if i % 2 == 0 else "MSFT",
                side="long" if i % 3 != 0 else "short",
                entry_price=100.0,
                exit_price=100.0 + net / 10.0,
                shares=10.0,
                position_value=1000.0,
                gross_pnl=net,
                net_pnl=net,
                return_pct=net / 1000.0 * 100,
                hold_days=20,  # multi-day holds — clears the avg-hold gate
                outcome="win" if net > 0 else "loss",
                cumulative_pnl=cum,
            )
        )
    return out


def _high_sharpe_metrics() -> BacktestResult:
    return BacktestResult(
        total_return_pct=80.0,
        annualized_return_pct=60.0,
        volatility_pct=8.0,
        sharpe_ratio=6.5,  # above the > 5.0 threshold
        max_drawdown_pct=4.0,
        win_rate_pct=60.0,
        profit_factor=2.4,
    )


def test_sharpe_above_5_is_critical_by_default():
    """Without ``dsr_aware``, ``Sharpe > 5.0`` stays critical so the
    single-window orchestrator path triggers refinement."""
    detector = BacktestAnomalyDetector()
    results = detector.check(_high_sharpe_metrics(), _trades())
    sharpe_failures = [r for r in results if not r.passed and "Sharpe ratio" in r.details]
    assert len(sharpe_failures) == 1
    assert sharpe_failures[0].severity == "critical"


def test_sharpe_above_5_downgrades_to_warning_when_dsr_aware():
    """With ``dsr_aware=True`` the same metrics produce a *warning*, not a
    critical — AcceptanceGate's OOS DSR is the authoritative check on this
    run."""
    detector = BacktestAnomalyDetector()
    results = detector.check(_high_sharpe_metrics(), _trades(), dsr_aware=True)
    sharpe_failures = [r for r in results if not r.passed and "Sharpe ratio" in r.details]
    assert len(sharpe_failures) == 1
    assert sharpe_failures[0].severity == "warning"
    assert "AcceptanceGate" in sharpe_failures[0].details


def test_dsr_aware_does_not_affect_other_critical_gates():
    """``dsr_aware`` only re-classifies the Sharpe band; other critical gates
    (zero trades, sub-1d holds, win-rate > 95%) stay critical."""
    metrics = BacktestResult(
        total_return_pct=10.0,
        annualized_return_pct=12.0,
        volatility_pct=8.0,
        sharpe_ratio=1.2,
        max_drawdown_pct=4.0,
        win_rate_pct=98.0,  # > 95 → critical
        profit_factor=1.4,
    )
    detector = BacktestAnomalyDetector()
    results = detector.check(metrics, _trades(), dsr_aware=True)
    critical = [r for r in results if not r.passed and r.severity == "critical"]
    assert any("Win rate" in r.details for r in critical)
