"""ConvergenceTracker tests — focused on trial_count (issue #247, step 4)."""

from __future__ import annotations

from collections import Counter

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.quality_gates.convergence_tracker import (
    ConvergenceTracker,
)
from investment_team.strategy_lab.quality_gates.models import QualityGateResult
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)


def _mk_spec(asset_class: str = "stocks") -> StrategySpec:
    return StrategySpec(
        strategy_id="s1",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="test hypothesis",
        signal_definition="close crosses above SMA(20)",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 5})
                )
            )
        ],
    )


def _passing_gate() -> QualityGateResult:
    return QualityGateResult(
        gate_name="dummy",
        passed=True,
        severity="info",
        phase="design",
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


# ----------------------------------------------------------------------
# Issue #552 — _strategy_signature determinism under the structured DSL.
# ----------------------------------------------------------------------


def _spec_with_entries(entry_rules, asset_class: str = "stocks") -> StrategySpec:
    return StrategySpec(
        strategy_id="s1",
        authored_by="test",
        asset_class=asset_class,
        hypothesis="test hypothesis",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=entry_rules,
        exit_rules=[
            SignalExitRule(
                when=Predicate(
                    lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 5})
                ),
            ),
        ],
    )


def test_strategy_signature_invariant_to_entry_list_order():
    """``_strategy_signature`` is invariant to the position of rules within
    ``entry_rules`` — guaranteed by the use of a ``Set`` for the returned
    tokens (and reinforced by the ``sorted((_canon(r) for r in ...))`` pass
    inside the helper)."""
    rule_a = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
        ),
    )
    rule_b = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 50})
        ),
    )

    sig_ab = ConvergenceTracker._strategy_signature(_spec_with_entries([rule_a, rule_b]))
    sig_ba = ConvergenceTracker._strategy_signature(_spec_with_entries([rule_b, rule_a]))

    assert sig_ab == sig_ba


def test_strategy_signature_canonicalises_across_distinct_equivalent_rules():
    """Two structurally identical rules built as separate Python objects
    must produce the same token. This is the property ``sort_keys=True``
    inside ``_canon`` exists to defend: a future change to dict-ordering
    semantics in Pydantic or stdlib ``json`` must not silently
    desynchronise the signature."""
    rule_v1 = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
        ),
    )
    rule_v2 = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
        ),
    )
    assert rule_v1 is not rule_v2

    sig_1 = ConvergenceTracker._strategy_signature(_spec_with_entries([rule_v1]))
    sig_2 = ConvergenceTracker._strategy_signature(_spec_with_entries([rule_v2]))

    assert sig_1 == sig_2


def test_strategy_signature_differs_when_rule_payload_differs():
    """A meaningful payload change — ``IndicatorRef(name="sma", params={"period": 20})`` vs
    ``IndicatorRef(name="sma", params={"period": 50})`` — must produce a different entry token. Guards
    against an over-aggressive canonicalisation that collapses distinct
    rules."""
    rule_20 = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 20})
        ),
    )
    rule_50 = EntryRule(
        side="long",
        when=Predicate(
            lhs="bar.close", op=">", rhs=IndicatorRef(name="sma", params={"period": 50})
        ),
    )

    sig_20 = ConvergenceTracker._strategy_signature(_spec_with_entries([rule_20]))
    sig_50 = ConvergenceTracker._strategy_signature(_spec_with_entries([rule_50]))

    assert sig_20 != sig_50


def test_strategy_signature_includes_asset_class_token():
    """``_strategy_signature`` emits an ``ac:<lower>`` token. Two specs with
    identical rules but different asset classes must differ exactly in that
    token."""
    sig_stocks = ConvergenceTracker._strategy_signature(_mk_spec("stocks"))
    sig_crypto = ConvergenceTracker._strategy_signature(_mk_spec("crypto"))

    assert sig_stocks ^ sig_crypto == {"ac:stocks", "ac:crypto"}


def _failing_gate(name: str = "trade_alignment") -> QualityGateResult:
    return QualityGateResult(
        gate_name=name,
        passed=False,
        severity="critical",
        phase="verification",
        details="",
    )


def test_record_dedupes_failures_by_gate_name_per_cycle():
    """Regression for PR #613 review: many failed rows under the same
    ``gate_name`` in a single cycle count as ONE cycle-level failure.

    ``DeterministicAlignmentChecker`` emits one row per trade × per
    check, so a single misaligned cycle can produce dozens of failing
    rows tagged ``alignment_finding``. ``_failure_modes`` is supposed
    to track cross-cycle failure frequency for
    ``get_failure_directives(min_occurrences=3)``; without deduping,
    one bad cycle with three failed findings would prematurely trip
    the directive.
    """
    t = ConvergenceTracker()
    # One cycle with 10 failing rows under the same gate name.
    rows = [_failing_gate("alignment_finding") for _ in range(10)]
    t.record(_mk_spec(), rows)
    assert t._failure_modes["alignment_finding"] == 1

    # Second cycle, same shape — count goes to 2, not 12.
    t.record(_mk_spec(), rows)
    assert t._failure_modes["alignment_finding"] == 2


def test_record_count_asset_class_false_skips_diversity_but_keeps_failure_tracking():
    """Short-circuited cycles (e.g. an unsupported asset_class routed to
    redesign) pass ``count_asset_class=False`` so a coerced ``stocks``
    placeholder can't skew ``get_diversity_directive`` toward a false "heavily
    stocks" signal — while signatures and failure modes are still recorded so
    stall and failure-frequency detection see the failed attempt."""
    t = ConvergenceTracker()
    # Five short-circuited "stocks"-coerced cycles must NOT trip the diversity
    # directive, because none of them count toward the asset-class history.
    for _ in range(5):
        t.record(_mk_spec("stocks"), [_failing_gate("spec_readiness")], count_asset_class=False)
    assert t._asset_class_history == []
    assert t.get_diversity_directive() is None
    # Signatures and failure modes are still tracked.
    assert len(t._signatures) == 5
    assert t._failure_modes["spec_readiness"] == 5

    # A subsequent real stocks cycle does count, and on its own three+ counts
    # would steer diversity — proving the skip only suppressed the placeholders.
    for _ in range(3):
        t.record(_mk_spec("stocks"), [_passing_gate()])
    assert t._asset_class_history == ["stocks", "stocks", "stocks"]
    directive = t.get_diversity_directive()
    assert directive is not None and "stocks" in directive


def test_record_counts_distinct_gate_names_separately_in_one_cycle():
    """Distinct gate names in the same cycle each count once."""
    t = ConvergenceTracker()
    t.record(
        _mk_spec(),
        [
            _failing_gate("trade_alignment"),
            _failing_gate("alignment_finding"),
            _failing_gate("alignment_finding"),  # dedupes
            _failing_gate("code_safety"),
        ],
    )
    assert t._failure_modes["trade_alignment"] == 1
    assert t._failure_modes["alignment_finding"] == 1
    assert t._failure_modes["code_safety"] == 1


def test_record_inserts_failed_gates_in_deterministic_order():
    """Regression for PR #613 review: failed gates with the same count
    must surface in deterministic order in ``get_failure_directives``.

    ``Counter.most_common`` breaks ties by first-seen insertion order;
    iterating an unordered set across runs would make equal-count
    directives appear in different orders, polluting ideation prompts
    and snapshot tests. The tracker now sorts the failed-gate names
    before incrementing the counter.
    """
    # Build two trackers from independently-shuffled gate-result lists
    # that share the same set of failing gate names. Insertion order
    # into _failure_modes must match because the tracker sorts.
    rows = [_failing_gate(name) for name in ["zeta_gate", "alpha_gate", "mu_gate", "beta_gate"]]
    rows_reversed = list(reversed(rows))

    t1 = ConvergenceTracker()
    t1.record(_mk_spec(), rows)
    t2 = ConvergenceTracker()
    t2.record(_mk_spec(), rows_reversed)

    # Same insertion order in both — sorted alphabetically.
    assert list(t1._failure_modes.keys()) == list(t2._failure_modes.keys())
    assert list(t1._failure_modes.keys()) == [
        "alpha_gate",
        "beta_gate",
        "mu_gate",
        "zeta_gate",
    ]
