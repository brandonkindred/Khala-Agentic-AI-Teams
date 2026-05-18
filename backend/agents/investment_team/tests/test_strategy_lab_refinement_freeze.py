"""Refinement spec-freeze regression suite (#543).

These tests lock in the post-#543 contract:
- ``RefinementAgent.run`` filters spec-mutating keys out of its output
  and records them on ``spec_mutation_history`` (with a logger warning).
- The orchestrator's ``_merge_risk_limits_tighten_only`` accepts
  tightening, rejects loosening, and discards unknown / immutable keys.
- ``_apply_updates`` raises ``SpecImplementabilityError`` on a
  risk-limits loosening attempt or after ``_SPEC_MUTATION_TRIP_THRESHOLD``
  consecutive stray-key rounds for the same ``failure_phase``.
- ``run_cycle`` re-enters ideation on ``SpecImplementabilityError`` and
  short-circuits with ``status='failed: spec_unimplementable'`` after
  exhausting ``MAX_DESIGN_REENTRIES``.
- The refinement loop preserves the original spec (``entry_rules``,
  ``hypothesis``, etc.) even when the LLM stub emits spec-mutating
  keys round after round.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pytest

from investment_team.execution.risk_filter import RiskLimits
from investment_team.market_data_service import OHLCVBar
from investment_team.models import (
    BacktestConfig,
    BacktestResult,
    StrategySpec,
)
from investment_team.strategy_lab import orchestrator as orchestrator_module
from investment_team.strategy_lab.agents.refinement import RefinementAgent
from investment_team.strategy_lab.exceptions import SpecImplementabilityError
from investment_team.strategy_lab.orchestrator import (
    MAX_DESIGN_REENTRIES,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)
from investment_team.trading_service.modes.sandbox_compat import StrategyRunResult

# Every test in this module drives `run_cycle` on a real
# StrategyLabOrchestrator; the marker auto-applies the readiness fetch
# stub from conftest. See conftest.py for the contract.
pytestmark = pytest.mark.strategy_lab_integration

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _config() -> BacktestConfig:
    return BacktestConfig(
        start_date="2023-01-01",
        end_date="2023-12-31",
        initial_capital=100_000.0,
        benchmark_symbol="SPY",
        transaction_cost_bps=5.0,
        slippage_bps=2.0,
        walk_forward_enabled=False,
    )


def _spec(strategy_id: str = "strat-freeze-test") -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        authored_by="test",
        asset_class="stocks",
        hypothesis="RSI mean reversion baseline",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": 5, "max_drawdown_pct": 10},
        speculative=False,
        strategy_code="# original code",
    )


# ---------------------------------------------------------------------------
# RefinementAgent output filtering
# ---------------------------------------------------------------------------


class _FakeStrandsAgentReturning:
    """Callable stub that mimics the strands ``Agent`` instance the
    refinement code instantiates inline. Always returns the same payload."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def __call__(self, prompt: str) -> str:
        return self._payload


def test_refinement_agent_filters_and_warns_on_spec_keys(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """LLM payloads carrying spec-mutating keys are narrowed before return."""
    payload = (
        "{"
        '"strategy_code": "# refined",'
        '"changes_made": "tightened RSI threshold",'
        '"entry_rules": ["bogus"],'
        '"hypothesis": "rewritten",'
        '"sizing_rules": ["bogus"]'
        "}"
    )
    fake_agent = _FakeStrandsAgentReturning(payload)

    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.refinement.Agent",
        lambda **kwargs: fake_agent,
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.refinement.get_strands_model",
        lambda role: object(),
    )

    agent = RefinementAgent()
    with caplog.at_level(logging.WARNING, logger="investment_team.strategy_lab.agents.refinement"):
        updates, new_code = agent.run(
            spec=_spec(),
            code="# original code",
            failure_phase="execution",
            failure_details="boom",
        )

    assert new_code == "# refined"
    assert set(updates) == {"changes_made"}
    assert updates["changes_made"] == "tightened RSI threshold"
    assert agent.spec_mutation_history == [
        {
            "failure_phase": "execution",
            "keys": ["entry_rules", "hypothesis", "sizing_rules"],
        }
    ]
    assert any("spec-mutating keys" in rec.message for rec in caplog.records)


def test_refinement_agent_passes_risk_limits_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``risk_limits`` is the lone passthrough so the orchestrator can apply
    its tighten-only carve-out."""
    payload = (
        "{"
        '"strategy_code": "# refined",'
        '"changes_made": "tighter sizing",'
        '"risk_limits": {"max_position_pct": 3}'
        "}"
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.refinement.Agent",
        lambda **kwargs: _FakeStrandsAgentReturning(payload),
    )
    monkeypatch.setattr(
        "investment_team.strategy_lab.agents.refinement.get_strands_model",
        lambda role: object(),
    )

    agent = RefinementAgent()
    updates, new_code = agent.run(
        spec=_spec(),
        code="# original",
        failure_phase="execution",
        failure_details="boom",
    )

    assert new_code == "# refined"
    assert set(updates) == {"changes_made", "risk_limits"}
    assert updates["risk_limits"] == {"max_position_pct": 3}
    # No stray keys → no history entry, no warning.
    assert agent.spec_mutation_history == []


# ---------------------------------------------------------------------------
# Risk-limits tighten-only merge
# ---------------------------------------------------------------------------


def test_merge_risk_limits_tightening_accepted() -> None:
    current = RiskLimits()  # default max_position_pct = 6.0
    merged, loosened, unknown = StrategyLabOrchestrator._merge_risk_limits_tighten_only(
        current, {"max_position_pct": 3.0}
    )
    assert loosened == []
    assert unknown == []
    assert merged.max_position_pct == 3.0


def test_merge_risk_limits_loosening_rejected() -> None:
    current = RiskLimits()
    _, loosened, unknown = StrategyLabOrchestrator._merge_risk_limits_tighten_only(
        current, {"max_position_pct": 99.0}
    )
    assert loosened == ["max_position_pct"]
    assert unknown == []


def test_merge_risk_limits_unknown_key_discarded() -> None:
    current = RiskLimits()
    merged, loosened, unknown = StrategyLabOrchestrator._merge_risk_limits_tighten_only(
        current, {"made_up_field": 0.1}
    )
    assert loosened == []
    assert unknown == ["made_up_field"]
    assert merged == current


def test_merge_risk_limits_immutable_key_discarded() -> None:
    """``vol_lookback_days`` is mapped to ``None`` direction → discard, not trip."""
    current = RiskLimits()
    _, loosened, unknown = StrategyLabOrchestrator._merge_risk_limits_tighten_only(
        current, {"vol_lookback_days": 50}
    )
    assert loosened == []
    assert unknown == ["vol_lookback_days"]


def test_merge_risk_limits_target_vol_none_to_value_is_loosening() -> None:
    """Going from None (flat sizing) to a vol target changes sizing model."""
    current = RiskLimits()
    assert current.target_annual_vol is None
    _, loosened, _ = StrategyLabOrchestrator._merge_risk_limits_tighten_only(
        current, {"target_annual_vol": 0.10}
    )
    assert loosened == ["target_annual_vol"]


# ---------------------------------------------------------------------------
# _apply_updates trip-threshold
# ---------------------------------------------------------------------------


def test_apply_updates_trips_after_threshold_consecutive_stray_keys() -> None:
    orch = StrategyLabOrchestrator()
    spec = _spec()
    stray = {"entry_rules": ["bogus"], "changes_made": "noop"}
    # Two rounds: stray emitted, no raise.
    orch._apply_updates(spec, stray, "# c1", failure_phase="execution")
    orch._apply_updates(spec, stray, "# c2", failure_phase="execution")
    # Third consecutive round trips.
    with pytest.raises(SpecImplementabilityError) as exc_info:
        orch._apply_updates(spec, stray, "# c3", failure_phase="execution")
    assert exc_info.value.failure_phase == "execution"
    assert exc_info.value.last_spec is spec
    assert exc_info.value.last_code == "# c3"


def test_apply_updates_resets_counter_on_clean_round() -> None:
    """A clean refinement round resets the per-phase counter."""
    orch = StrategyLabOrchestrator()
    spec = _spec()
    stray = {"entry_rules": ["bogus"]}
    orch._apply_updates(spec, stray, "# c1", failure_phase="execution")
    orch._apply_updates(spec, stray, "# c2", failure_phase="execution")
    # Clean round resets.
    orch._apply_updates(spec, {"changes_made": "ok"}, "# c3", failure_phase="execution")
    # Now two more stray rounds should NOT trip (counter reset).
    orch._apply_updates(spec, stray, "# c4", failure_phase="execution")
    orch._apply_updates(spec, stray, "# c5", failure_phase="execution")
    # Third consecutive trips.
    with pytest.raises(SpecImplementabilityError):
        orch._apply_updates(spec, stray, "# c6", failure_phase="execution")


def test_apply_updates_interleaved_phases_do_not_trip() -> None:
    """Threshold is per-phase consecutive; interleaving phases resets counters."""
    orch = StrategyLabOrchestrator()
    spec = _spec()
    stray = {"entry_rules": ["bogus"]}
    orch._apply_updates(spec, stray, "# c1", failure_phase="execution")
    orch._apply_updates(spec, stray, "# c2", failure_phase="validation")
    orch._apply_updates(spec, stray, "# c3", failure_phase="execution")
    # No phase has 3 consecutive yet — no raise.


# ---------------------------------------------------------------------------
# run_cycle re-entry loop
# ---------------------------------------------------------------------------


class _FakeIdeationAgent:
    """Returns a fixed ideation tuple every call."""

    def __init__(self, spec: StrategySpec) -> None:
        self._spec = spec
        self.call_count = 0

    def run(
        self,
        *,
        prior_records: List[Any],
        signal_brief: Any = None,
        convergence_directives: Optional[List[str]] = None,
        exclude_asset_classes: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str, str]:
        self.call_count += 1
        strategy_dict = {
            "asset_class": self._spec.asset_class,
            "hypothesis": self._spec.hypothesis,
            "signal_definition": self._spec.signal_definition,
            "entry_rules": [r.model_dump() for r in self._spec.entry_rules],
            "exit_rules": [r.model_dump() for r in self._spec.exit_rules],
            "sizing": self._spec.sizing.model_dump(),
            "risk_limits": self._spec.risk_limits.model_dump(),
            "target_symbols": list(self._spec.target_symbols),
        }
        return strategy_dict, "# ideation code", "test rationale"


class _LoosenOnceRefinementAgent:
    """Stub that always emits a risk-limits loosening proposal so the
    orchestrator's ``_apply_updates`` raises immediately."""

    def __init__(self) -> None:
        self.spec_mutation_history: List[Dict[str, Any]] = []

    def run(
        self,
        spec: StrategySpec,
        code: str,
        failure_phase: str,
        failure_details: str,
        metrics: Optional[BacktestResult] = None,
        prior_attempts: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        return (
            {
                "changes_made": "loosen sizing",
                "risk_limits": {"max_position_pct": 99.0},
            },
            "# refined code",
        )


class _StraySpecRefinementAgent:
    """Stub that always emits stray spec-mutating keys (no risk-limits
    loosening). After ``_SPEC_MUTATION_TRIP_THRESHOLD`` consecutive rounds
    on the same ``failure_phase`` the orchestrator's ``_apply_updates``
    trips ``SpecImplementabilityError`` via the threshold path."""

    def __init__(self) -> None:
        self.spec_mutation_history: List[Dict[str, Any]] = []
        self.call_count = 0

    def run(
        self,
        spec: StrategySpec,
        code: str,
        failure_phase: str,
        failure_details: str,
        metrics: Optional[BacktestResult] = None,
        prior_attempts: Optional[List[str]] = None,
    ) -> Tuple[Dict[str, Any], str]:
        self.call_count += 1
        return (
            {
                "changes_made": f"stray attempt {self.call_count}",
                "entry_rules": [{"side": "long", "comment": "bogus"}],
                "hypothesis": "rewritten by LLM",
            },
            f"# refined code {self.call_count}",
        )


def test_run_cycle_reroutes_then_short_circuits_on_persistent_loosening(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every design attempt's refinement tries to loosen risk limits,
    ``run_cycle`` emits loopback events and persists a
    ``failed: spec_unimplementable`` record after exhaustion."""
    spec = _spec()
    orch = StrategyLabOrchestrator()
    orch.ideation_agent = _FakeIdeationAgent(spec)  # type: ignore[assignment]
    orch.refinement_agent = _LoosenOnceRefinementAgent()  # type: ignore[assignment]

    # Make code-safety pass so the loop reaches refinement.
    monkeypatch.setattr(
        orch.code_safety_checker,
        "check",
        lambda code, spec=None: [],
    )
    # Pre-synthesis spec validator passes.
    monkeypatch.setattr(
        orch.strategy_validator,
        "validate",
        lambda s: [],
    )

    # Force execution to fail so the refinement path fires.
    def _failed_run(
        code: str, market_data: Any, config: Any, strategy: Any = None
    ) -> StrategyRunResult:
        return StrategyRunResult(
            success=False,
            error_type="runtime_error",
            stderr="forced failure for test",
            stdout="",
        )

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _failed_run)

    # Stub market-data fetch so the loop has data to "execute" against.
    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec_arg, config_arg: orchestrator_module._MarketDataFetch(
            data={
                "SPY": [
                    OHLCVBar(
                        symbol="SPY", date="2023-01-01", open=1, high=1, low=1, close=1, volume=1
                    )
                ]
            },
            requested_symbols=["SPY"],
            fetched_symbols=["SPY"],
        ),
    )

    emitted: List[Tuple[str, Dict[str, Any]]] = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: emitted.append((phase, data)),
    )

    loopback_events = [
        d for phase, d in emitted if phase == "ideating" and d.get("sub_phase") == "loopback"
    ]
    assert len(loopback_events) == MAX_DESIGN_REENTRIES
    assert orch.ideation_agent.call_count == MAX_DESIGN_REENTRIES + 1  # type: ignore[attr-defined]
    assert record.backtest.status == "failed: spec_unimplementable"
    # PR #573 round-5 Note 2: short-circuit records must populate
    # ``acceptance_reason`` so a reader of the persisted record sees
    # the rejection cause without having to inspect ``status`` or
    # ``quality_gate_results`` separately.
    ar = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in ar and "spec_unimplementable" in ar, (
        f"expected publication_disabled / spec_unimplementable in {ar!r}"
    )


def test_run_cycle_reroutes_on_stray_key_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end coverage for the threshold-trip path (non-risk-limits):
    when refinement emits stray spec keys for ``_SPEC_MUTATION_TRIP_THRESHOLD``
    consecutive rounds on the same ``failure_phase``, ``run_cycle``
    re-enters ideation and ultimately persists a
    ``failed: spec_unimplementable`` record."""
    spec = _spec()
    orch = StrategyLabOrchestrator()
    orch.ideation_agent = _FakeIdeationAgent(spec)  # type: ignore[assignment]
    orch.refinement_agent = _StraySpecRefinementAgent()  # type: ignore[assignment]

    monkeypatch.setattr(orch.code_safety_checker, "check", lambda code, spec=None: [])
    monkeypatch.setattr(orch.strategy_validator, "validate", lambda s: [])

    # Force every execution to fail so the loop stays in the "execution"
    # failure_phase and accumulates stray-key rounds against a single
    # phase counter (the threshold is per-phase consecutive).
    def _failed_run(
        code: str, market_data: Any, config: Any, strategy: Any = None
    ) -> StrategyRunResult:
        return StrategyRunResult(
            success=False,
            error_type="runtime_error",
            stderr="forced failure for test",
            stdout="",
        )

    monkeypatch.setattr(orchestrator_module, "run_strategy_code", _failed_run)
    monkeypatch.setattr(
        orch,
        "_fetch_market_data",
        lambda spec_arg, config_arg: orchestrator_module._MarketDataFetch(
            data={
                "SPY": [
                    OHLCVBar(
                        symbol="SPY", date="2023-01-01", open=1, high=1, low=1, close=1, volume=1
                    )
                ]
            },
            requested_symbols=["SPY"],
            fetched_symbols=["SPY"],
        ),
    )

    emitted: List[Tuple[str, Dict[str, Any]]] = []
    record = orch.run_cycle(
        prior_records=[],
        config=_config(),
        on_phase=lambda phase, data: emitted.append((phase, data)),
    )

    loopback_events = [
        d for phase, d in emitted if phase == "ideating" and d.get("sub_phase") == "loopback"
    ]
    assert len(loopback_events) == MAX_DESIGN_REENTRIES
    # Each loopback's evidence references the threshold-path message, not
    # risk-limits loosening — confirms we exercised the right code path.
    for ev in loopback_events:
        assert "consecutive mutation attempts" in ev["evidence"]
        assert ev["failure_phase"] == "execution"
    assert orch.ideation_agent.call_count == MAX_DESIGN_REENTRIES + 1  # type: ignore[attr-defined]
    assert record.backtest.status == "failed: spec_unimplementable"
    assert "spec_unimplementable" in record.backtest.status
    # PR #573 round-5 Note 2: short-circuit records must populate
    # ``acceptance_reason`` for the same audit-trail reason.
    ar = record.backtest.result.acceptance_reason or ""
    assert "publication_disabled" in ar and "spec_unimplementable" in ar, (
        f"expected publication_disabled / spec_unimplementable in {ar!r}"
    )
