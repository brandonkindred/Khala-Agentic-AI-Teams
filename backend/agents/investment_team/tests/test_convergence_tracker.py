"""ConvergenceTracker tests — focused on trial_count (issue #247, step 4)."""

from __future__ import annotations

from collections import Counter

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


# ----------------------------------------------------------------------
# Issue #269 — merge_from parallel-wave trial-count fold-back
# ----------------------------------------------------------------------


def test_merge_from_adds_cycle_delta_not_full_count():
    primary = ConvergenceTracker()
    primary.increment_trials(2)
    snap = primary.snapshot()  # baseline captured at 2
    snap.increment_trials(5)  # cycle did 5 refinement rounds

    primary.merge_from(snap)

    # 2 (pre-wave) + 5 (delta), not 2 + 7 which would double-count the baseline.
    assert primary.trial_count == 7


def test_merge_from_on_fresh_primary_adds_full_trial_count():
    # Issue #269 AC: primary.merge_from(snapshot_with_5_trials) → trial_count += 5
    # when the baseline is 0 (freshly constructed primary).
    primary = ConvergenceTracker()
    snap = primary.snapshot()
    snap.increment_trials(5)

    primary.merge_from(snap)
    assert primary.trial_count == 5


def test_merge_from_does_not_touch_diversity_state():
    # Diversity merging is the wave-completion ``record()`` loop's job;
    # merge_from must stay out of it to avoid double-counting.
    primary = ConvergenceTracker()
    primary.record(_mk_spec("stocks"), [_passing_gate()])

    pre_signatures = list(primary._signatures)
    pre_history = list(primary._asset_class_history)
    pre_failures = Counter(primary._failure_modes)

    snap = primary.snapshot()
    snap.record(_mk_spec("crypto"), [_passing_gate()])  # cycle adds diversity
    snap.increment_trials(3)

    primary.merge_from(snap)

    assert primary._signatures == pre_signatures
    assert primary._asset_class_history == pre_history
    assert primary._failure_modes == pre_failures
    assert primary.trial_count == 3  # delta only


def test_merge_from_multiple_snapshots_accumulate_like_a_parallel_wave():
    # Mirrors the orchestration call pattern: one primary, N sibling
    # snapshots built at the same time, each does K refinement rounds, all
    # merged back at wave end.
    primary = ConvergenceTracker()
    snaps = [primary.snapshot() for _ in range(3)]
    for s in snaps:
        s.increment_trials(4)

    for s in snaps:
        primary.merge_from(s)

    assert primary.trial_count == 12


def test_merge_from_rejects_shrinking_snapshot():
    primary = ConvergenceTracker()
    primary.increment_trials(10)
    snap = primary.snapshot()
    # Manually corrupt trial_count below the captured baseline.
    snap._trial_count = 3

    with pytest.raises(ValueError, match="monotonic"):
        primary.merge_from(snap)


def test_merge_from_directly_constructed_tracker_uses_zero_baseline():
    # A tracker built without snapshot() has ``_trial_count_at_snapshot = 0``;
    # merge_from folds its full trial_count. Useful for tests that construct
    # synthetic trackers directly.
    primary = ConvergenceTracker()
    other = ConvergenceTracker()
    other.increment_trials(8)

    primary.merge_from(other)
    assert primary.trial_count == 8


def test_merge_from_lowers_dsr_on_subsequent_cycle():
    """Issue #269 AC: DSR regression — after a parallel-batch wave merges
    sibling trial counts into the primary, DSR computed on a subsequent
    cycle at the same raw Sharpe is strictly lower than the pre-merge DSR.

    This is the end-to-end motivation for the fix: merge_from propagates
    trial counts into the primary so that n_trials passed to
    ``compute_deflated_sharpe`` reflects all sibling work, not just prior
    waves."""
    from investment_team.execution.metrics import compute_deflated_sharpe

    sharpe = 1.5
    n_obs = 252

    # Pre-merge: primary sits at whatever trial_count prior waves produced
    # (simulated here as a modest baseline). DSR on the next cycle sees
    # n_trials = primary.trial_count + 1 (this cycle).
    primary = ConvergenceTracker()
    primary.increment_trials(5)  # prior waves
    dsr_pre_merge = compute_deflated_sharpe(
        sharpe=sharpe, n_trials=primary.trial_count + 1, n_obs=n_obs
    )

    # Simulate a parallel wave of 3 sibling cycles, each doing 30 refinement
    # rounds on its own snapshot. Without merge_from these 90 trials would
    # be invisible to DSR on the next cycle.
    snaps = [primary.snapshot() for _ in range(3)]
    for s in snaps:
        s.increment_trials(30)
    for s in snaps:
        primary.merge_from(s)

    assert primary.trial_count == 5 + 3 * 30  # merge_from landed the delta
    dsr_post_merge = compute_deflated_sharpe(
        sharpe=sharpe, n_trials=primary.trial_count + 1, n_obs=n_obs
    )

    assert dsr_post_merge < dsr_pre_merge, (
        "Expected post-merge DSR to deflate further once sibling trial "
        f"counts are visible; got pre={dsr_pre_merge:.6f} post={dsr_post_merge:.6f}"
    )
