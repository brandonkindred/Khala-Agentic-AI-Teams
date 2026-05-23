"""Cost-stress realism gate.

Overfit strategies often look great at retail friction (1× transaction-cost
and slippage assumptions) but collapse the moment costs rise. Without an
enforced cost-stress sweep, a strategy whose edge is entirely synthesized
by under-modelled friction can still pass the acceptance gate and be
marked winning.

This gate consumes ``BacktestResult.cost_stress_results`` (populated by
:mod:`investment_team.trading_service.modes.backtest` when
``BacktestConfig.cost_stress=True``) and enforces two contracts:

* **Mandatory cost-stress on winning-candidate runs** — when the run is on
  the winning-candidate path (``walk_forward_enabled=True``), cost-stress
  must have been requested and must have produced results.
* **Sharpe floor at 2× friction** — when results are present, the 2.0×
  multiplier row's Sharpe ratio must be ``>= 0``. A strategy that
  collapses to negative Sharpe at twice the modelled costs is not robust
  enough to ship.

The gate is wired from
:meth:`StrategyLabOrchestrator._run_realism_gates`. Critical findings
veto ``is_winning`` via the standard
:func:`_apply_veto_to_acceptance_reason` path.
"""

from __future__ import annotations

from typing import Any, ClassVar, Iterable, List, Mapping, Optional

from ...models import BacktestConfig, BacktestResult
from .models import GateResultsMixin, QualityGateResult, StrategyLabPhase

GATE = "cost_stress_realism"

# 2.0× is the canonical "twice retail" stress level. The multipliers list
# in BacktestConfig defaults to [1.0, 2.0, 3.0]; 2.0 is the floor consumers
# pin acceptance criteria against. Match the existing executor tolerance
# (``CostStressReport.at`` uses ``1e-6``) so a stored multiplier of
# ``2.0000001`` still resolves.
_TARGET_MULTIPLIER: float = 2.0
_MULTIPLIER_TOL: float = 1e-6


class CostStressRealismGate(GateResultsMixin):
    """Verification-phase gate over the cost-stress sweep payload.

    Contract:
      Pre: ``metrics`` is a :class:`BacktestResult` (post walk-forward);
      ``config`` is the run's :class:`BacktestConfig`.
      Post: returns one or more :class:`QualityGateResult`s tagged with
      the caller's ``phase``. The gate is deterministic over the inputs —
      no LLM calls, no I/O. ``critical`` results indicate a publication
      veto; ``warning`` indicates a soft alert that does not block.
      Invariants: never returns an empty list.
    """

    GATE: ClassVar[str] = GATE

    def check(
        self,
        metrics: BacktestResult,
        config: BacktestConfig,
        *,
        phase: StrategyLabPhase = "verification",
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            results: List[QualityGateResult] = []
            payload = metrics.cost_stress_results

            if not payload:
                results.append(self._handle_missing_results(config))
                return results

            row_2x = _row_for_multiplier(payload, _TARGET_MULTIPLIER)
            if row_2x is None:
                results.append(
                    self._warning(
                        f"Cost-stress sweep missing the {_TARGET_MULTIPLIER:g}× "
                        "multiplier row — cannot verify Sharpe floor at twice "
                        "retail friction. Configure "
                        "BacktestConfig.cost_stress_multipliers to include 2.0."
                    )
                )
                return results

            sharpe = row_2x.get("sharpe_ratio")
            if sharpe is None:
                results.append(
                    self._warning(
                        f"Cost-stress {_TARGET_MULTIPLIER:g}× row present but "
                        "sharpe_ratio is missing — cannot evaluate the floor."
                    )
                )
                return results
            if sharpe < 0:
                results.append(
                    self._critical(
                        f"Sharpe ratio at {_TARGET_MULTIPLIER:g}× costs is "
                        f"{sharpe:.2f} < 0 — strategy's edge does not survive "
                        "twice the modelled friction."
                    )
                )
                return results

            results.append(
                self._info(
                    f"Cost-stress sweep clean at {_TARGET_MULTIPLIER:g}× (Sharpe {sharpe:.2f})."
                )
            )
            return results

    def _handle_missing_results(self, config: BacktestConfig) -> QualityGateResult:
        """Emit the appropriate result when ``cost_stress_results`` is absent.

        Preconditions:
          - ``self._using_phase(...)`` is active.
        Postconditions:
          - Returns exactly one :class:`QualityGateResult`.
          - ``critical`` when the run was on a winning-candidate path
            (``walk_forward_enabled=True``) — mandatory cost-stress was not
            run, which is the explicit failure mode this gate enforces.
            Also ``critical`` when the operator asked for cost-stress
            (``config.cost_stress=True``) but no results came back — that
            means the engine silently dropped the sweep, which is a bug we
            want surfaced rather than swallowed.
          - ``info`` (passing) on legacy single-window runs that explicitly
            opt out of walk-forward; the realism cycle isn't responsible
            for those, the acceptance gate is.
        Invariants:
          - Never returns ``warning`` for the missing case — either the
            absence matters enough to veto, or it doesn't matter at all.
        """
        if config.walk_forward_enabled or config.cost_stress:
            return self._critical(
                "Cost-stress sweep not executed for a winning-candidate run. "
                "Set BacktestConfig.cost_stress=True so the realism cycle can "
                "verify the strategy's edge survives 2× friction."
            )
        return self._info(
            "Cost-stress sweep not executed; walk_forward_enabled=False so "
            "no winning-candidate verdict is at stake."
        )


def _row_for_multiplier(rows: Iterable[Any], multiplier: float) -> Optional[Mapping[str, Any]]:
    """Return the first row whose ``multiplier`` matches ``multiplier``
    within :data:`_MULTIPLIER_TOL`, or ``None`` if no row matches.

    Preconditions:
      - ``rows`` is iterable; each element is a mapping. The persistence
        layer flattens :class:`CostStressRow` to dicts via ``to_payload``
        before storage, so this is the only shape the gate ever sees
        through :class:`BacktestResult`.
    Postconditions:
      - Returns a mapping the caller can index by string key.
      - Returns ``None`` when ``rows`` is empty, no element is a mapping,
        the multiplier field is missing or unparseable, or no row's
        multiplier is within tolerance.
    """
    for row in rows or ():
        if not isinstance(row, Mapping):
            continue
        row_mult = row.get("multiplier")
        if row_mult is None:
            continue
        try:
            if abs(float(row_mult) - multiplier) <= _MULTIPLIER_TOL:
                return row
        except (TypeError, ValueError):
            continue
    return None


__all__ = ["GATE", "CostStressRealismGate"]
