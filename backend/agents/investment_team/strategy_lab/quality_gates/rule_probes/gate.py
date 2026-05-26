"""The :class:`RuleProbesGate` quality-gate facade.

For every entry/exit rule in the spec the gate:

1. Asks :func:`generate_rule_probe_runs` to build a deterministic OHLCV
   sequence designed to fire that rule's predicate.
2. Runs the compiled ``strategy_code`` through :func:`run_strategy_code`
   against the synthetic bars with a tiny ``initial_capital`` and zero
   transaction costs so sizing/order behaviour is deterministic.
3. Asks :func:`assess_probe` to compare the resulting ``TradeRecord``
   envelope against the rule's :class:`ExpectedOutcome`.

The gate is purely deterministic — every probe uses the same fixed bar
geometry, no randomness, no LLM calls. Per-probe runtime is bounded by
the sandbox's subprocess spawn (~0.2-1.0 s per probe on local hardware),
satisfying the ``< 5 s per probe`` budget in the gate's contract.

Routing on failure: critical probe results join the synthesis loop's
existing ``critical_failures`` collection in :mod:`orchestrator` and
route through ``_refine_or_exhaust(failure_phase="validation", ...)``.
The probe's ``rule_id`` is surfaced inside the aggregated
``failure_details`` string so the refinement agent can target the right
branch.

Limitations:

- ``SignalExitRule`` probes assert the closing trade carries an
  ``exit_reason`` substring of ``"signal_exit"``. The deterministic
  compiler emits ``reason="compiled_signal_exit"`` which matches.
  Strategies that close via strategy-emitted orders with a different
  reason string (legacy LLM-authored code) will fail this probe even
  when correct; the asserter renders a clear diagnostic so the
  refinement loop can adjust.
- Synthesis is heuristic for ATR/ADX/Stochastic thresholds; recipes
  that can't satisfy the predicate within the binary-search budget
  return ``synthesizable=False`` and surface as warnings.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, List, Optional

from ....models import BacktestConfig
from ...spec_dsl import StopLossRule, TakeProfitRule
from ..models import GateResultsMixin, QualityGateResult, StrategyLabPhase
from .asserter import assess_probe
from .synthesizer import ProbeRun, generate_rule_probe_runs

logger = logging.getLogger(__name__)

GATE: str = "rule_probes"

# Probe-specific BacktestConfig knobs. Tiny capital makes sizing math
# deterministic regardless of the strategy's chosen sizing rule.
_PROBE_INITIAL_CAPITAL = 1_000.0
_PROBE_TRANSACTION_COST_BPS = 0.0
_PROBE_SLIPPAGE_BPS = 0.0


class RuleProbesGate(GateResultsMixin):
    """Behavioural complement to :class:`CodeConformanceGate`.

    Run **after** ``CodeConformanceGate`` in the synthesis loop. The
    orchestrator should gate this call on conformance passing — probing
    code that already failed structural checks adds noisy ``rule_id``
    criticals on top of the cleaner conformance critical.

    Contract:
      Pre: ``code`` is non-empty Python source; ``spec`` is a
      ``StrategySpec`` whose ``entry_rules`` and ``exit_rules`` describe
      the expected rule set.
      Post: returned list has one :class:`QualityGateResult` per spec
      rule. ``rule_id`` is set on every result so the orchestrator can
      thread the failing rule into ``failure_details``.
    """

    GATE: ClassVar[str] = GATE

    def __init__(self, runner: Optional[Any] = None) -> None:
        """``runner`` is the sandbox entry point — defaults to the real
        :func:`run_strategy_code`. Tests inject a stub to assert against
        recorded calls without spawning subprocesses.
        """
        if runner is None:
            from ....trading_service.modes.sandbox_compat import run_strategy_code as _rsc

            runner = _rsc
        self._runner = runner

    def check(
        self,
        code: str,
        spec: Any,
        *,
        phase: StrategyLabPhase = "synthesis",
    ) -> List[QualityGateResult]:
        with self._using_phase(phase):
            if not code or not code.strip():
                return [self._critical("Rule probes gate received empty strategy_code.")]
            probes = generate_rule_probe_runs(spec, compiled_code=code)
            if not probes:
                return [
                    self._info(
                        "No entry/exit rules to probe — gate has no work to do."
                    )
                ]
            results: List[QualityGateResult] = []
            for probe in probes:
                result = self._run_probe(probe, code, spec)
                results.append(assess_probe(probe, result, emitter=self))
            return results

    def _run_probe(self, probe: ProbeRun, code: str, spec: Any) -> Any:
        """Invoke the sandbox for one probe.

        Skips the sandbox entirely when the probe is unprobeable — the
        asserter handles that path without needing a result.
        """
        if not probe.synthesizable:
            return _SkippedResult()
        config = self._make_probe_config(probe)
        market_data = {probe.symbol: probe.market_data}
        try:
            return self._runner(code, market_data, config, strategy=spec)
        except Exception as exc:
            logger.warning(
                "RuleProbesGate sandbox raised for rule_id=%s: %s", probe.rule_id, exc
            )
            return _FailedResult(error_type="probe_runner_exception", stderr=str(exc))

    def _make_probe_config(self, probe: ProbeRun) -> BacktestConfig:
        bars = probe.market_data
        # Pre: synthesizable probes always carry at least 2 bars.
        assert bars, "synthesizable probe must carry at least one bar"
        return BacktestConfig(
            start_date=bars[0].date,
            end_date=bars[-1].date,
            initial_capital=_PROBE_INITIAL_CAPITAL,
            transaction_cost_bps=_PROBE_TRANSACTION_COST_BPS,
            slippage_bps=_PROBE_SLIPPAGE_BPS,
            min_signals_per_bar=0.0,
        )


class _SkippedResult:
    """Stand-in for ``StrategyRunResult`` when a probe was not run."""

    success = True
    trades: List[Any] = []
    error_type = None
    stderr = ""


class _FailedResult:
    """Stand-in for ``StrategyRunResult`` when the sandbox raised."""

    success = False
    trades: List[Any] = []

    def __init__(self, *, error_type: str, stderr: str) -> None:
        self.error_type = error_type
        self.stderr = stderr


# ``StopLossRule`` and ``TakeProfitRule`` are imported so the package's
# re-export surface stays minimal but the module can be inspected from
# the gate's call site (e.g. for type-narrowing in tests).
__all__ = ["GATE", "RuleProbesGate", "StopLossRule", "TakeProfitRule"]
