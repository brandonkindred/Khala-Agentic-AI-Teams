"""Composite OOS acceptance gate for the Strategy Lab (issue #247).

Replaces the legacy ``WINNING_THRESHOLD = 8.0`` annualized-return scalar with
a four-criteria check that runs on walk-forward OOS results:

1. OOS Deflated Sharpe Ratio ≥ ``config.dsr_threshold``
2. IS → OOS Sharpe degradation ≤ ``config.max_is_oos_degradation_pct``
3. OOS trade count ≥ ``config.min_oos_trades``
4. Regime-conditional pass: beats the configured benchmark in at least
   ``min_regime_beats`` of the regime subwindows (default 2 of 4)

The orchestrator composes this gate only when walk-forward evaluation ran.
When required inputs are missing, a single warning-level result is emitted so
the orchestrator's ``all(passed)`` acceptance check correctly rejects an
incomplete evaluation rather than silently passing.
"""

from __future__ import annotations

from typing import List, Optional

from ...models import BacktestConfig, BacktestResult
from .models import QualityGateResult

GATE = "acceptance_gate"

DEFAULT_MIN_REGIME_BEATS = 2


class AcceptanceGate:
    """Composite OOS acceptance gate built on walk-forward diagnostics."""

    def __init__(self, min_regime_beats: int = DEFAULT_MIN_REGIME_BEATS):
        if min_regime_beats < 0:
            raise ValueError(f"min_regime_beats must be >= 0, got {min_regime_beats}")
        self._min_regime_beats = min_regime_beats

    def check(
        self,
        result: BacktestResult,
        config: BacktestConfig,
        *,
        n_trials: Optional[int] = None,
    ) -> List[QualityGateResult]:
        """Return one ``QualityGateResult`` per sub-criterion.

        ``n_trials`` is accepted for future use by gate implementations that
        want to adjust the DSR threshold based on cumulative trial count; the
        DSR itself is already deflated upstream.
        """
        missing: List[str] = []
        if result.oos_sharpe is None:
            missing.append("oos_sharpe")
        if result.is_oos_degradation_pct is None:
            missing.append("is_oos_degradation_pct")
        if result.oos_trade_count is None:
            missing.append("oos_trade_count")
        if missing:
            return [
                QualityGateResult(
                    gate_name=GATE,
                    passed=False,
                    severity="warning",
                    details=(
                        "Acceptance gate cannot evaluate — walk-forward diagnostics "
                        f"missing: {', '.join(missing)}."
                    ),
                )
            ]

        results: List[QualityGateResult] = []

        # 1. Deflated Sharpe threshold.
        dsr = float(result.deflated_sharpe)
        passed_dsr = dsr >= config.dsr_threshold
        results.append(
            QualityGateResult(
                gate_name=GATE,
                passed=passed_dsr,
                severity="info" if passed_dsr else "critical",
                details=(
                    f"OOS DSR {dsr:.3f} "
                    f"{'meets' if passed_dsr else 'below'} threshold "
                    f"{config.dsr_threshold:.3f}"
                    + (f" (n_trials={n_trials})" if n_trials is not None else "")
                ),
            )
        )

        # 2. IS → OOS degradation.
        deg = float(result.is_oos_degradation_pct)
        passed_deg = deg <= config.max_is_oos_degradation_pct
        results.append(
            QualityGateResult(
                gate_name=GATE,
                passed=passed_deg,
                severity="info" if passed_deg else "critical",
                details=(
                    f"IS→OOS Sharpe degradation {deg:.1f}% "
                    f"{'within' if passed_deg else 'exceeds'} "
                    f"{config.max_is_oos_degradation_pct:.1f}% ceiling"
                ),
            )
        )

        # 3. OOS trade count.
        n_oos = int(result.oos_trade_count)
        passed_count = n_oos >= config.min_oos_trades
        results.append(
            QualityGateResult(
                gate_name=GATE,
                passed=passed_count,
                severity="info" if passed_count else "critical",
                details=(
                    f"OOS trade count {n_oos} "
                    f"{'meets' if passed_count else 'below'} minimum "
                    f"{config.min_oos_trades}"
                ),
            )
        )

        # 4. Regime-conditional pass.
        regime_results = result.regime_results or []
        beats = sum(1 for r in regime_results if r.get("beat_benchmark"))
        total = len(regime_results)
        passed_regime = beats >= self._min_regime_beats
        regime_detail = (
            f"Beat benchmark in {beats} of {total} regime subwindows "
            f"(threshold: {self._min_regime_beats})"
            if total
            else "No regime subwindows evaluated"
        )
        results.append(
            QualityGateResult(
                gate_name=GATE,
                passed=passed_regime,
                severity="info" if passed_regime else "critical",
                details=regime_detail,
            )
        )

        return results


def summarize_acceptance_reason(results: List[QualityGateResult]) -> str:
    """Human-readable summary of the composite gate outcome.

    Returns ``"all four criteria met"`` on full pass; otherwise a
    comma-separated list of the failing sub-gate detail strings.
    """
    fails = [r for r in results if not r.passed]
    if not fails:
        return "all four criteria met"
    return "; ".join(r.details for r in fails)
