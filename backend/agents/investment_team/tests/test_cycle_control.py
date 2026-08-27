"""Coverage for ``strategy_lab.cycle_control``.

``gather_convergence_directives`` and ``require_short_circuit_inputs`` are
``run_cycle``'s directive-gathering and terminal-guard logic, extracted into
their own dependency-minimal module so both thread-mode ``run_cycle`` and the
Temporal-mode ``StrategyLabCycleWorkflow.run`` (via ``temporal/dto.py``'s
adapters) share one implementation. See ``cycle_control.py``'s module
docstring for why this logic can't live alongside the rest of ``run_cycle``'s
extracted helpers in ``_orchestrator_helpers.py``.
"""

from __future__ import annotations

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.cycle_control import (
    gather_convergence_directives,
    require_short_circuit_inputs,
)
from investment_team.strategy_lab.quality_gates.convergence_tracker import ConvergenceTracker
from investment_team.strategy_lab.quality_gates.models import QualityGateResult


def _spec(**overrides) -> StrategySpec:
    fields = dict(
        strategy_id="cycle-control-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
    )
    fields.update(overrides)
    return StrategySpec(**fields)


def _gate(
    *,
    name: str = "some_gate",
    passed: bool = True,
    severity: str = "info",
    details: str = "",
    phase: str = "synthesis",
) -> QualityGateResult:
    return QualityGateResult(
        gate_name=name,
        passed=passed,
        details=details,
        severity=severity,
        phase=phase,
    )


# ---------------------------------------------------------------------------
# gather_convergence_directives — run_cycle's directive-gathering, extracted
# ---------------------------------------------------------------------------


def test_gather_convergence_directives_empty_for_fresh_tracker() -> None:
    tracker = ConvergenceTracker()
    assert gather_convergence_directives(tracker) == []


def test_gather_convergence_directives_orders_stall_diversity_failure() -> None:
    tracker = ConvergenceTracker(window_size=5)
    failing_gate = _gate(name="risk_gate", passed=False, severity="critical")
    for _ in range(5):
        tracker.record(_spec(), [failing_gate])

    directives = gather_convergence_directives(tracker)

    assert len(directives) == 3
    assert directives[0].startswith("WARNING: Strategy ideation is converging")
    assert "heavily skewed toward stocks" in directives[1]
    assert "Gate 'risk_gate' has failed 5 times" in directives[2]


# ---------------------------------------------------------------------------
# require_short_circuit_inputs — run_cycle's terminal guard, extracted
# ---------------------------------------------------------------------------


def test_require_short_circuit_inputs_passes_when_both_present() -> None:
    require_short_circuit_inputs(_spec(), "some evidence")


@pytest.mark.parametrize(
    "last_spec, last_evidence",
    [
        (None, "some evidence"),
        (_spec(), None),
        (None, None),
    ],
)
def test_require_short_circuit_inputs_raises_when_missing(last_spec, last_evidence) -> None:
    with pytest.raises(
        RuntimeError,
        match="SpecImplementabilityError raised without last_spec/evidence",
    ):
        require_short_circuit_inputs(last_spec, last_evidence)


def test_require_short_circuit_inputs_behaves_identically_for_wire_dict_spec() -> None:
    """The Temporal workflow passes a wire dict, not a ``StrategySpec``, for
    ``last_spec`` — the guarded ``is None`` check must behave identically."""
    require_short_circuit_inputs({"strategy_id": "wire-dict"}, "some evidence")
    with pytest.raises(RuntimeError, match="SpecImplementabilityError raised without"):
        require_short_circuit_inputs(None, "some evidence")
