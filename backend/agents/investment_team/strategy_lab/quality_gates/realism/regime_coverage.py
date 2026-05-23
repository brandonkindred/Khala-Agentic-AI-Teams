"""Regime coverage realism gate.

A strategy that only "works" in one market regime is brittle — the next
regime change (vol spike, low-vol drift, trending vs. mean-reverting) is
likely to invert its edge. The Strategy Lab's per-regime evaluator
(:func:`investment_team.strategy_lab.orchestrator._evaluate_regimes`)
already partitions the strategy's daily return series by VIX quartile
and records ``strategy_cumret`` per regime on
``BacktestResult.regime_results``. This gate reads that payload and
enforces two contracts:

* **Critical** — any regime the strategy actually participated in
  (``n_obs > 0``) produced a negative compounded return
  (``strategy_cumret < 0``). The strategy demonstrably loses money in
  that regime; shipping it is shipping a known-bad behaviour.
* **Warning** — the strategy participated in only one regime out of the
  four labels in :data:`REGIME_LABELS`. The realism cycle doesn't
  punish single-regime strategies outright (some strategies are
  designed for one regime), but the operator should know the run
  didn't sample the full market.

The gate is skipped (info) when ``regime_results`` is missing or empty
— the orchestrator's regime evaluator handles that case defensively
(returns ``[]`` on internal exceptions or insufficient bars).
"""

from __future__ import annotations

from typing import Any, ClassVar, List, Sequence

from ....models import BacktestResult
from ..models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "regime_coverage_realism"


class RegimeCoverageGate(GateResultsMixin):
    """Verification-phase gate over per-regime strategy returns.

    Contract:
      Pre: ``metrics`` is a :class:`BacktestResult` whose
      ``regime_results`` field (when present) is a list of dicts in the
      shape :func:`regime_comparison` produces — ``{regime, n_obs,
      strategy_cumret, benchmark_cumret, beat_benchmark}``.
      Post: returns one or more :class:`QualityGateResult`s tagged with
      the caller's ``phase``. ``critical`` results indicate a
      publication veto; ``warning`` indicates a soft alert.
      Invariants: deterministic over its inputs; never returns an empty
      list; treats unknown payload shapes as info-skip rather than
      crashing.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        metrics: BacktestResult,
        *,
        phase: StrategyLabPhase = "verification",
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            payload = metrics.regime_results
            if not payload:
                return [
                    self._info(
                        "Regime coverage check skipped: regime_results missing "
                        "or empty (insufficient OOS data for regime evaluation)."
                    )
                ]

            covered = _covered_regimes(payload)
            losing = _losing_regimes(payload)

            results: List[QualityGateResult] = []
            if losing:
                losers_fmt = ", ".join(f"{label} ({ret:+.2%})" for label, ret in losing)
                results.append(
                    self._critical(
                        f"Strategy posted negative compounded returns in regime(s) "
                        f"{losers_fmt} — shipping the strategy ships a known-bad "
                        "behaviour in those market conditions."
                    )
                )

            if not covered:
                results.append(
                    self._info(
                        "Regime coverage check: no regime has any observations "
                        "(every regime's ``n_obs`` was zero)."
                    )
                )
            elif len(covered) == 1:
                only = next(iter(covered))
                results.append(
                    self._warning(
                        f"Strategy only participated in regime {only!r} — the "
                        "OOS sample didn't exercise the other regimes, so the "
                        "realism cycle can't verify out-of-regime robustness."
                    )
                )
            elif not losing:
                results.append(
                    self._info(
                        f"Regime coverage check clean: {len(covered)} regime(s) "
                        f"covered, all with non-negative compounded returns."
                    )
                )

            return results


def _covered_regimes(payload: Sequence[Any]) -> set:
    """Return the set of regime labels with at least one observation.

    Preconditions:
      - ``payload`` is iterable of mappings; each mapping has ``regime``
        and ``n_obs`` keys (the shape ``regime_comparison`` emits).
    Postconditions:
      - Entries lacking ``regime`` or ``n_obs``, or with ``n_obs <= 0``,
        are silently skipped.
    """
    out = set()
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        label = entry.get("regime")
        n_obs = entry.get("n_obs")
        if not isinstance(label, str):
            continue
        if isinstance(n_obs, (int, float)) and n_obs > 0:
            out.add(label)
    return out


def _losing_regimes(payload: Sequence[Any]) -> List[tuple]:
    """Return ``[(regime_label, strategy_cumret)]`` for regimes the
    strategy traded in and lost money in.

    Preconditions:
      - ``payload`` is iterable of mappings shaped like
        ``regime_comparison``'s output.
    Postconditions:
      - Only regimes with ``n_obs > 0`` AND ``strategy_cumret < 0`` are
        included.
      - Entries with non-numeric ``strategy_cumret`` are silently
        skipped (treated as "no signal").
    Invariants:
      - Output order matches input order so the rendered message reads
        in regime-order on the standard ``vix_q1 → vix_q4`` payload.
    """
    out: List[tuple] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        label = entry.get("regime")
        n_obs = entry.get("n_obs")
        ret = entry.get("strategy_cumret")
        if not isinstance(label, str):
            continue
        if not isinstance(n_obs, (int, float)) or n_obs <= 0:
            continue
        if not isinstance(ret, (int, float)):
            continue
        if ret < 0:
            out.append((label, float(ret)))
    return out


__all__ = ["GATE", "RegimeCoverageGate"]
