"""Threshold-based anomaly detector for backtest results."""

from __future__ import annotations

from typing import List

from ...models import BacktestResult, TradeRecord
from .models import QualityGateResult

GATE = "backtest_anomaly"


class BacktestAnomalyDetector:
    """Flag backtest results that are statistically implausible or likely buggy."""

    def check(
        self,
        metrics: BacktestResult,
        trades: List[TradeRecord],
    ) -> List[QualityGateResult]:
        results: List[QualityGateResult] = []

        # 1. Zero trades
        if not trades:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="critical",
                details="Backtest produced zero trades — strategy code never entered a position.",
            ))
            return results

        # 2. Too few trades
        if len(trades) < 5:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="critical",
                details=f"Only {len(trades)} trades — statistically meaningless for a multi-year backtest.",
            ))

        # 3. Annualized return > 200%
        if metrics.annualized_return_pct > 200:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="critical",
                details=f"Annualized return {metrics.annualized_return_pct:.1f}% is suspiciously high (>200%) — likely a data or logic bug.",
            ))

        # 4. Win rate thresholds
        if metrics.win_rate_pct > 95:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="critical",
                details=f"Win rate {metrics.win_rate_pct:.1f}% exceeds 95% — almost certainly overfitting or lookahead bias.",
            ))
        elif metrics.win_rate_pct > 90:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="warning",
                details=f"Win rate {metrics.win_rate_pct:.1f}% exceeds 90% — review for possible overfitting.",
            ))

        # 5. Extreme profit factor
        if metrics.profit_factor > 10:
            results.append(QualityGateResult(
                gate_name=GATE, passed=False, severity="critical",
                details=f"Profit factor {metrics.profit_factor:.1f} exceeds 10 — likely data snooping or bug.",
            ))

        # 6. Average hold time < 1 day (possible lookahead)
        if trades:
            avg_hold = sum(t.hold_days for t in trades) / len(trades)
            if avg_hold < 1:
                results.append(QualityGateResult(
                    gate_name=GATE, passed=False, severity="warning",
                    details=f"Average hold time {avg_hold:.1f} days — sub-day holds may indicate lookahead bias in daily data.",
                ))

        # 7. Single trade concentration
        if trades:
            total_pnl = sum(abs(t.net_pnl) for t in trades)
            if total_pnl > 0:
                max_single = max(abs(t.net_pnl) for t in trades)
                if max_single / total_pnl > 0.5:
                    results.append(QualityGateResult(
                        gate_name=GATE, passed=False, severity="warning",
                        details=f"Largest single trade is {max_single / total_pnl:.0%} of total absolute P&L — high concentration risk.",
                    ))

        # 8. All trades identical direction and symbol
        if len(trades) > 1:
            sides = {t.side for t in trades}
            symbols = {t.symbol for t in trades}
            if len(sides) == 1 and len(symbols) == 1:
                results.append(QualityGateResult(
                    gate_name=GATE, passed=False, severity="warning",
                    details=f"All {len(trades)} trades are {next(iter(sides))} on {next(iter(symbols))} — no diversification.",
                ))

        if not results:
            results.append(QualityGateResult(
                gate_name=GATE, passed=True, severity="info",
                details="Backtest results passed all anomaly checks.",
            ))

        return results
