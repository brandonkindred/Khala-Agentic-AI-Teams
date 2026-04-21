"""ConvergenceTracker tests — focused on trial_count (issue #247, step 4)."""

from __future__ import annotations

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.convergence_tracker import (
    ConvergenceTracker,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult


def _mk_spec(asset_class: str = "stocks") -> StrategySpec:
    return StrategySpec(
        strategy_id="s1",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="test hypothesis",
        signal_definition="close crosses above SMA(20)",
        entry_rules=["close > sma(20)"],
        exit_rules=["close < sma(5)"],
    )


def _passing_gate() -> QualityGateResult:
    return QualityGateResult(
        gate_name="dummy",
        passed=True,
        severity="info",
        details="",
    )


def test_trial_count_starts_at_zero():
    t = ConvergenceTracker()
    assert t.trial_count == 0


def test_increment_trials_accumulates():
    t = ConvergenceTracker()
    t.increment_trials(3)
    t.increment_trials(7)
    assert t.trial_count == 10


def test_increment_trials_default_is_one():
    t = ConvergenceTracker()
    t.increment_trials()
    t.increment_trials()
    assert t.trial_count == 2


def test_increment_trials_rejects_negative():
    t = ConvergenceTracker()
    with pytest.raises(ValueError, match="non-negative"):
        t.increment_trials(-1)


def test_record_does_not_implicitly_increment_trials():
    t = ConvergenceTracker()
    t.record(_mk_spec(), [_passing_gate()])
    t.record(_mk_spec(), [_passing_gate()])
    # Diversity signatures accumulate, trial_count does not — the orchestrator
    # increments trials separately after each refinement loop.
    assert t.trial_count == 0


def test_snapshot_carries_trial_count_but_is_independent():
    primary = ConvergenceTracker()
    primary.increment_trials(5)
    snap = primary.snapshot()
    assert snap.trial_count == 5
    snap.increment_trials(10)
    # Snapshot is a deep-enough copy that mutations don't leak back.
    assert snap.trial_count == 15
    assert primary.trial_count == 5


def test_snapshot_preserves_diversity_state():
    primary = ConvergenceTracker()
    primary.record(_mk_spec("crypto"), [_passing_gate()])
    primary.record(_mk_spec("stocks"), [_passing_gate()])
    primary.increment_trials(4)

    snap = primary.snapshot()
    assert snap.trial_count == 4
    # Diversity directives should see the same history.
    assert snap._asset_class_history == ["crypto", "stocks"]
    assert len(snap._signatures) == 2
