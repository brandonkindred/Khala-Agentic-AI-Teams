"""Coverage for ``StrategyLabOrchestrator._refine_or_exhaust`` preconditions.

The precondition checks were previously bare ``assert`` statements, silently
disabled under ``python -O``. They are now explicit ``if``/``raise`` guards
that must always fire, regardless of interpreter optimization flags.
"""

from __future__ import annotations

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.orchestrator import (
    RefinementStallTracker,
    StrategyLabOrchestrator,
)
from investment_team.strategy_lab.spec_dsl import EntryRule, IndicatorRef, Predicate, SignalExitRule


def _spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-precondition-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
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
        risk_limits={"max_position_pct": 5},
        speculative=False,
        strategy_code="from contract import Strategy\n\nclass S(Strategy):\n    pass\n",
    )


def _valid_kwargs() -> dict:
    return dict(
        spec=_spec(),
        code="from contract import Strategy\n\nclass S(Strategy):\n    pass\n",
        failure_phase="validation",
        failure_details="details",
        metrics=None,
        refinement_attempts=[],
        round_num=0,
        default_change_label="refined",
        emit=lambda *a, **k: None,
        stall_tracker=RefinementStallTracker(),
    )


def test_refine_or_exhaust_rejects_non_spec_spec() -> None:
    orch = StrategyLabOrchestrator()
    kwargs = _valid_kwargs()
    kwargs["spec"] = {"not": "a spec"}
    with pytest.raises(TypeError, match="spec must be a StrategySpec"):
        orch._refine_or_exhaust(**kwargs)


def test_refine_or_exhaust_rejects_non_str_code() -> None:
    orch = StrategyLabOrchestrator()
    kwargs = _valid_kwargs()
    kwargs["code"] = 12345
    with pytest.raises(TypeError, match="code must be a string"):
        orch._refine_or_exhaust(**kwargs)


@pytest.mark.parametrize("bad_failure_phase", ["", None, 42])
def test_refine_or_exhaust_rejects_empty_or_non_str_failure_phase(bad_failure_phase) -> None:
    orch = StrategyLabOrchestrator()
    kwargs = _valid_kwargs()
    kwargs["failure_phase"] = bad_failure_phase
    with pytest.raises(ValueError, match="failure_phase must be a non-empty string"):
        orch._refine_or_exhaust(**kwargs)


def test_refine_or_exhaust_rejects_negative_round_num() -> None:
    orch = StrategyLabOrchestrator()
    kwargs = _valid_kwargs()
    kwargs["round_num"] = -1
    with pytest.raises(ValueError, match="round_num must be non-negative"):
        orch._refine_or_exhaust(**kwargs)
