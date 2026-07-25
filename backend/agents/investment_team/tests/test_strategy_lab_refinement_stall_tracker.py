"""Unit tests for the code-refinement loop's stall tracker.

Mirrors ``test_strategy_lab_critique_ledger.py``'s stall-predicate coverage,
but for ``RefinementStallTracker`` — a rolling ``(code_hash, failure_hash)``
signature window rather than an open-issue-id set.
"""

from __future__ import annotations

import pytest

from investment_team.strategy_lab._orchestrator_helpers import RefinementStallTracker


def test_not_stalled_with_insufficient_history() -> None:
    tracker = RefinementStallTracker()
    tracker.record("code-a", "failure-a")
    tracker.record("code-a", "failure-a")
    # Only 2 identical rounds; threshold of 3 -> not yet stalled.
    assert tracker.is_stalled(3) is False


def test_stalled_when_signature_unchanged_for_n_rounds() -> None:
    tracker = RefinementStallTracker()
    for _ in range(3):
        tracker.record("code-a", "failure-a")
    assert tracker.is_stalled(3) is True


def test_changing_code_hash_breaks_stall() -> None:
    tracker = RefinementStallTracker()
    tracker.record("code-a", "failure-a")
    tracker.record("code-b", "failure-a")
    tracker.record("code-c", "failure-a")
    assert tracker.is_stalled(3) is False


def test_changing_failure_hash_breaks_stall() -> None:
    tracker = RefinementStallTracker()
    tracker.record("code-a", "failure-a")
    tracker.record("code-a", "failure-b")
    tracker.record("code-a", "failure-c")
    assert tracker.is_stalled(3) is False


def test_stall_threshold_floored_to_one() -> None:
    tracker = RefinementStallTracker()
    tracker.record("code-a", "failure-a")
    # n<1 floors to 1 -> a single recorded round counts as stalled.
    assert tracker.is_stalled(0) is True


def test_rounds_recorded_counts_every_record_call() -> None:
    tracker = RefinementStallTracker()
    assert tracker.rounds_recorded == 0
    tracker.record("code-a", "failure-a")
    tracker.record("code-b", "failure-b")
    assert tracker.rounds_recorded == 2


def test_older_rounds_outside_window_do_not_count() -> None:
    tracker = RefinementStallTracker()
    tracker.record("code-a", "failure-a")  # outside the last-3 window below
    tracker.record("code-b", "failure-b")
    tracker.record("code-b", "failure-b")
    tracker.record("code-b", "failure-b")
    assert tracker.is_stalled(3) is True
    assert tracker.is_stalled(4) is False


def test_two_cycle_oscillation_is_detected_as_stall_with_enough_history() -> None:
    """An A/B/A/B... oscillating signature is recognized as a period-2 stall
    once the window holds at least two full cycles (n >= 4); smaller windows
    lack enough evidence to distinguish oscillation from real progress."""
    tracker = RefinementStallTracker()
    for i in range(6):
        if i % 2 == 0:
            tracker.record("code-a", "failure-a")
        else:
            tracker.record("code-b", "failure-b")
    # Six rounds recorded, alternating A/B/A/B/A/B. Windows of size 2 or 3
    # don't contain two full period-2 cycles, so they're not (yet) flagged.
    assert tracker.is_stalled(2) is False
    assert tracker.is_stalled(3) is False
    # A window of 4 contains exactly two period-2 cycles (B,A,B,A / A,B,A,B
    # depending on parity) — recognized as a short-period oscillating stall.
    assert tracker.is_stalled(4) is True


def test_three_cycle_oscillation_is_detected_as_stall() -> None:
    """A period-3 A/B/C/A/B/C... oscillation is detected once two full
    cycles (n >= 6) are in the window."""
    tracker = RefinementStallTracker()
    signatures = [("code-a", "failure-a"), ("code-b", "failure-b"), ("code-c", "failure-c")]
    for i in range(6):
        code_hash, failure_hash = signatures[i % 3]
        tracker.record(code_hash, failure_hash)
    assert tracker.is_stalled(5) is False
    assert tracker.is_stalled(6) is True


def test_identical_window_still_detected_over_longer_oscillating_history() -> None:
    """Once an oscillation settles into a true repeat, the shortest matching
    period (1, i.e. identical) is still found within a bounded window."""
    tracker = RefinementStallTracker()
    for _ in range(2):
        tracker.record("code-a", "failure-a")
        tracker.record("code-b", "failure-b")
    for _ in range(3):
        tracker.record("code-c", "failure-c")
    assert tracker.is_stalled(3) is True


# ---------------------------------------------------------------------------
# _refinement_stall_rounds env resolver
# ---------------------------------------------------------------------------


def test_refinement_stall_rounds_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_refinement_stall_rounds`` defaults to 3, parses overrides, floors
    sub-1 to 1, and falls back to 3 on garbage — same contract as
    ``_design_review_stall_rounds``, applied to the refinement loop."""
    from investment_team.strategy_lab.orchestrator import _refinement_stall_rounds

    monkeypatch.delenv("STRATEGY_LAB_REFINEMENT_STALL_ROUNDS", raising=False)
    assert _refinement_stall_rounds() == 3

    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_STALL_ROUNDS", "5")
    assert _refinement_stall_rounds() == 5

    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_STALL_ROUNDS", "0")
    assert _refinement_stall_rounds() == 1

    monkeypatch.setenv("STRATEGY_LAB_REFINEMENT_STALL_ROUNDS", "garbage")
    assert _refinement_stall_rounds() == 3
