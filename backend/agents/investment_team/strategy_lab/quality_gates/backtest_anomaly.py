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
        *,
        mode: str = "backtest",
    ) -> List[QualityGateResult]:
        """Run anomaly checks.

        ``mode="backtest"`` (default) runs the full gate set; ``mode="paper"``
        relaxes gates that assume a multi-year backtest window so short
        paper-trading sessions don't false-trigger on "too few trades".
        """
        results: List[QualityGateResult] = []

        # 1. Zero trades (always flagged — even in paper mode a non-trading
        # strategy is a hard failure).
        if not trades:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details="Backtest produced zero trades — strategy code never entered a position.",
                )
            )
            return results

        # 2. Too few trades — backtest-only: paper sessions run over short
        # windows (a few weeks at most) so a <5-trade minimum is
        # inappropriate.  The signals_per_bar floor in BacktestConfig
        # is the paper-mode equivalent.
        if mode != "paper" and len(trades) < 5:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=f"Only {len(trades)} trades — statistically meaningless for a multi-year backtest.",
                )
            )

        # 3. Annualized return > 200%
        if metrics.annualized_return_pct > 200:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=f"Annualized return {metrics.annualized_return_pct:.1f}% is suspiciously high (>200%) — likely a data or logic bug.",
                )
            )

        # 4. Win rate thresholds
        if metrics.win_rate_pct > 95:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=f"Win rate {metrics.win_rate_pct:.1f}% exceeds 95% — almost certainly overfitting or lookahead bias.",
                )
            )
        elif metrics.win_rate_pct > 90:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=f"Win rate {metrics.win_rate_pct:.1f}% exceeds 90% — review for possible overfitting.",
                )
            )

        # 5. Extreme profit factor
        if metrics.profit_factor > 10:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="critical",
                    details=f"Profit factor {metrics.profit_factor:.1f} exceeds 10 — likely data snooping or bug.",
                )
            )

        # 5b. Sharpe ratio thresholds.
        # Issue #247: DSR (on walk-forward OOS) is now the authoritative
        # overfitting signal, applied by AcceptanceGate. The raw-Sharpe bands
        # remain as a cheap warning-level sanity check for single-window paths
        # and legacy callers that don't populate deflated_sharpe.
        if metrics.sharpe_ratio > 5.0:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=(
                        f"Sharpe ratio {metrics.sharpe_ratio:.2f} exceeds 5.0 — "
                        "possible look-ahead bias or calculation artifact. "
                        "When available, AcceptanceGate's OOS Deflated Sharpe "
                        "is the authoritative overfitting check."
                    ),
                )
            )
        elif metrics.sharpe_ratio > 3.0:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=f"Sharpe ratio {metrics.sharpe_ratio:.2f} exceeds 3.0 — review for overfitting or data snooping.",
                )
            )

        # 6. Average hold time < 1 day → hard fail on daily bars (Phase 2).
        if trades:
            avg_hold = sum(t.hold_days for t in trades) / len(trades)
            if avg_hold < 1:
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="critical",
                        details=(
                            f"Average hold time {avg_hold:.1f} days — sub-day holds "
                            "on daily-bar data are a strong indicator of look-ahead "
                            "bias or intra-bar execution that cannot be replicated live."
                        ),
                    )
                )

        # 7. Single trade concentration
        if trades:
            total_pnl = sum(abs(t.net_pnl) for t in trades)
            if total_pnl > 0:
                max_single = max(abs(t.net_pnl) for t in trades)
                if max_single / total_pnl > 0.5:
                    results.append(
                        QualityGateResult(
                            gate_name=GATE,
                            passed=False,
                            severity="warning",
                            details=f"Largest single trade is {max_single / total_pnl:.0%} of total absolute P&L — high concentration risk.",
                        )
                    )

        # 8. All trades identical direction and symbol
        if len(trades) > 1:
            sides = {t.side for t in trades}
            symbols = {t.symbol for t in trades}
            if len(sides) == 1 and len(symbols) == 1:
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="warning",
                        details=f"All {len(trades)} trades are {next(iter(sides))} on {next(iter(symbols))} — no diversification.",
                    )
                )

        # 9. Cost sensitivity — edge consumed by transaction costs
        if trades:
            gross_wins = sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
            gross_losses = abs(sum(t.gross_pnl for t in trades if t.gross_pnl <= 0))
            gross_pf = gross_wins / gross_losses if gross_losses > 0 else 0.0
            if gross_pf > 1.0 and metrics.profit_factor < 1.0:
                results.append(
                    QualityGateResult(
                        gate_name=GATE,
                        passed=False,
                        severity="warning",
                        details=(
                            f"Profit factor drops from {gross_pf:.2f} (gross) to "
                            f"{metrics.profit_factor:.2f} (net) — strategy edge is consumed by transaction costs."
                        ),
                    )
                )

        if not results:
            results.append(
                QualityGateResult(
                    gate_name=GATE,
                    passed=True,
                    severity="info",
                    details="Backtest results passed all anomaly checks.",
                )
            )

        return results
