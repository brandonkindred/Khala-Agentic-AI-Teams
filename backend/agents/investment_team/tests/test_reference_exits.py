"""Unit tests for ``executor.reference_exits`` (``StopLossRule``,
take-profit-family, and ``SignalExitRule`` modelling)."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.executor.predicate_evaluator import PandasHistoryView
from investment_team.strategy_lab.executor.reference_entries import (
    ReferenceEntryFill,
    bars_to_frame,
)
from investment_team.strategy_lab.executor.reference_exits import (
    ReferenceSignalExit,
    ReferenceStopLossExit,
    ReferenceTakeProfitExit,
    _PrefixHistoryView,
    entry_price_basis,
    replay_signal_exits,
    replay_stop_loss_exits,
    replay_take_profit_family_exits,
    resolve_signal_exit,
    resolve_stop_loss_exit,
    resolve_take_profit_family_exit,
    scaled_take_profit_rules,
    signal_exit_rules,
    stop_loss_rules_for_side,
    take_profit_rules,
    working_exit_rules,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    ExitRule,
    IndicatorRef,
    OcoBracketRule,
    Predicate,
    ScaledTakeProfitRule,
    SignalExitRule,
    StopLossRule,
    TakeProfitLevel,
    TakeProfitRule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Bar:
    """Minimal ``Bar``-shaped stand-in — the module only reads these attrs.

    Same shape ``test_reference_entries.py`` uses, so a bar fixture is portable
    between the entry-side and exit-side suites.
    """

    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0
    timestamp: str = "2024-01-01T00:00:00"
    symbol: str = "AAA"


def _bar(
    open_: float, high: float, low: float, close: float, ts: str = "2024-01-01T00:00:00"
) -> _Bar:
    """Positional OHLC factory, in OHLC order.

    Names are spelled out and consistent so a positional call reads correctly
    without opening this body; ``open_`` carries the underscore only to avoid
    shadowing the builtin.
    """
    return _Bar(open=open_, high=high, low=low, close=close, timestamp=ts)


def _flat(price: float, ts: str = "2024-01-01T00:00:00") -> _Bar:
    """A do-nothing bar: OHLC all at ``price``, so it can never trigger a stop
    that is not already at the price."""
    return _bar(price, price, price, price, ts)


def _entry(
    side: str = "long", entry_bar: int = 1, symbol: str = "AAA", price: float = 100.0
) -> ReferenceEntryFill:
    """A filled entry at ``price`` on bar ``entry_bar``.

    ``price`` is load-bearing, not incidental: at the suite's default
    ``entry_slippage_bps=0`` it becomes the post-slippage anchor every stop
    level and trailing watermark hangs off, so ``100.0`` is what makes the
    round percentages in these tests land on round levels (a 5% stop at 95).
    ``entry_bar=1`` leaves bar 0 free as pre-entry history and makes bar 2 the
    first bar a resting stop is eligible on. ``entry_date`` is never read by
    the exit model — only ``exit_date`` is derived, from the fill bar.
    """
    return ReferenceEntryFill(
        symbol=symbol,
        side=side,
        entry_bar=entry_bar,
        entry_date="2024-01-01",
        entry_rule_index=0,
        entry_price=price,
    )


def _spec(
    exit_rules: list[ExitRule] | None = None,
    entry_side: str = "long",
    target_symbols: list[str] | None = None,
) -> StrategySpec:
    """Standard test spec: enters when ``bar.close > 100``.

    ``exit_rules`` is annotated with the ``ExitRule`` union rather than
    ``StopLossRule``, because callers here also pass ``TakeProfitRule`` and
    ``OcoBracketRule`` to exercise the non-stop-rule paths — matching what
    ``StrategySpec.exit_rules`` itself declares.
    """
    return StrategySpec(
        strategy_id="strat-ref-exits-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(side=entry_side, when=Predicate(lhs="bar.close", op=">", rhs=100.0))
        ],
        exit_rules=exit_rules or [],
        target_symbols=target_symbols or [],
    )


# ---------------------------------------------------------------------------
# entry_price_basis — the post-slippage anchor
# ---------------------------------------------------------------------------


def test_zero_slippage_anchor_is_the_rounded_open():
    assert entry_price_basis(100.0, "long", 0.0) == 100.0
    assert entry_price_basis(100.0, "short", 0.0) == 100.0


def test_anchor_moves_against_the_position_side():
    """A long pays up, a short is filled down — the two must not share a sign."""
    assert entry_price_basis(100.0, "long", 200.0) == 102.0
    assert entry_price_basis(100.0, "short", 200.0) == 98.0


def test_anchor_rounds_to_four_places_below_ten_and_two_at_or_above():
    assert entry_price_basis(9.999999, "long", 0.0) == 10.0
    assert entry_price_basis(10.123456, "long", 0.0) == 10.12


def test_anchor_multiplies_before_rounding():
    """Production derives the bid and the slipped fill as two INDEPENDENT
    roundings of one raw price. Rounding first and scaling second differs in the
    last place near a bucket boundary, which is enough to move a stop level
    across a bar's extreme."""
    raw, bps = 9.99995, 2.0
    multiply_then_round = entry_price_basis(raw, "long", bps)
    round_then_multiply = round(round(raw, 4) * (1 + bps / 10_000), 4)
    assert multiply_then_round == pytest.approx(10.0019)
    assert multiply_then_round != round_then_multiply


@pytest.mark.parametrize("raw_open", [0.0, -1.0, float("nan"), float("inf")])
def test_anchor_rejects_nonpositive_or_nonfinite_open(raw_open):
    with pytest.raises(ValueError, match="raw_open"):
        entry_price_basis(raw_open, "long", 0.0)


def test_anchor_rejects_bad_side():
    with pytest.raises(ValueError, match="side"):
        entry_price_basis(100.0, "sideways", 0.0)


@pytest.mark.parametrize(
    ("raw_open", "side", "bps"),
    [
        (0.00004, "long", 0.0),  # sub-bucket price: round(0.00004, 4) == 0.0
        (0.00004, "short", 0.0),
        (0.5, "short", 9999.0),  # driven under the bucket by extreme slippage
    ],
)
def test_anchor_rejects_a_price_that_rounds_away_to_zero(raw_open, side, bps):
    """``raw_open > 0`` does not imply the ROUNDED anchor is positive.

    Silently returning ``0.0`` here would be invisible downstream: every stop
    level hangs off the anchor, so all of them collapse to zero, the
    nonpositive-fill guard suppresses each candidate fill, and the position
    never closes — the ledger emits no trade at all, which the matching module
    would read as a spec/engine divergence rather than a degenerate price.
    """
    with pytest.raises(ValueError, match="non-positive"):
        entry_price_basis(raw_open, side, bps)


def test_anchor_accepts_the_smallest_price_its_bucket_can_represent():
    """The guard rejects only what genuinely rounds away — one tick above the
    bucket's resolution still resolves."""
    assert entry_price_basis(0.0001, "long", 0.0) == 0.0001


@pytest.mark.parametrize("bps", [-1.0, 10_000.0, 20_000.0, float("nan"), float("inf")])
def test_anchor_rejects_out_of_range_slippage(bps):
    """At or above 10_000 bps the short-side multiplier hits zero or goes
    negative, producing a non-positive anchor and a sign-inverted level."""
    with pytest.raises(ValueError, match="entry_slippage_bps"):
        entry_price_basis(100.0, "long", bps)


# ---------------------------------------------------------------------------
# working_exit_rules — the engine-injected short safety stop
# ---------------------------------------------------------------------------


def test_long_only_spec_gets_no_injected_stop():
    spec = _spec(entry_side="long")
    assert working_exit_rules(spec) == []


def test_short_spec_without_a_short_stop_gets_the_injected_safety_stop():
    spec = _spec(entry_side="short")
    rules = working_exit_rules(spec)
    assert len(rules) == 1
    injected = rules[0]
    assert isinstance(injected, StopLossRule)
    assert (injected.pct, injected.basis) == (1.0, "entry_price")


def test_injected_stop_lands_at_index_len_exit_rules():
    """Its index is real and indexable — a production close through it is
    attributed at exactly this index, so the reference model must agree."""
    authored = TakeProfitRule(pct=0.1)
    spec = _spec(exit_rules=[authored], entry_side="short")
    rules = working_exit_rules(spec)
    assert len(rules) == 2
    assert isinstance(rules[1], StopLossRule)
    assert rules.index(rules[1]) == len(spec.exit_rules)


def test_existing_short_stop_suppresses_the_injection():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)], entry_side="short")
    assert len(working_exit_rules(spec)) == 1


def test_trailing_low_stop_suppresses_the_injection_but_trailing_high_does_not():
    """A ``trailing_high`` stop cannot fire for a short, so it is not an
    effective short-side stop and must not suppress the safety injection."""
    with_low = _spec(exit_rules=[StopLossRule(pct=0.05, basis="trailing_low")], entry_side="short")
    with_high = _spec(
        exit_rules=[StopLossRule(pct=0.05, basis="trailing_high")], entry_side="short"
    )
    assert len(working_exit_rules(with_low)) == 1
    assert len(working_exit_rules(with_high)) == 2


def test_bracket_stop_leg_suppresses_the_injection():
    spec = _spec(
        exit_rules=[OcoBracketRule(stop_loss={"pct": 0.05}, take_profit={"pct": 0.1})],
        entry_side="short",
    )
    assert len(working_exit_rules(spec)) == 1


def test_working_exit_rules_rejects_a_custom_code_spec():
    """The documented precondition, enforced rather than trusted.

    A ``requires_custom_code`` spec's real entries come from LLM-authored
    strategy code, not ``spec.entry_rules``, so replaying its rules would
    produce a ledger unrelated to what production traded — a confidently wrong
    oracle that raises nothing. Fail at the boundary instead.
    """
    spec = _spec()
    spec.requires_custom_code = True
    with pytest.raises(ValueError, match="requires_custom_code"):
        working_exit_rules(spec)


def test_replay_rejects_a_custom_code_spec():
    """The guard reaches the public entry point too, not just the helper."""
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    spec.requires_custom_code = True
    with pytest.raises(ValueError, match="requires_custom_code"):
        replay_stop_loss_exits(spec, {"AAA": [_flat(101.0), _flat(100.0)]})


def test_working_exit_rules_does_not_mutate_the_spec():
    spec = _spec(entry_side="short")
    before = list(spec.exit_rules)
    working_exit_rules(spec)
    assert list(spec.exit_rules) == before == []


# ---------------------------------------------------------------------------
# stop_loss_rules_for_side
# ---------------------------------------------------------------------------


def test_only_side_compatible_stops_are_candidates():
    rules = [
        TakeProfitRule(pct=0.1),  # not a stop at all
        StopLossRule(pct=0.05, basis="trailing_low"),  # short-only
        StopLossRule(pct=0.03, basis="entry_price"),  # both sides
        StopLossRule(pct=0.04, basis="trailing_high"),  # long-only
    ]
    assert [i for i, _ in stop_loss_rules_for_side(rules, "long")] == [2, 3]
    assert [i for i, _ in stop_loss_rules_for_side(rules, "short")] == [1, 2]


# ---------------------------------------------------------------------------
# style="market", basis="entry_price"
# ---------------------------------------------------------------------------


def _resolve(rules, bars, side="long", entry_bar=1, **kw):
    return resolve_stop_loss_exit(rules, _entry(side=side, entry_bar=entry_bar), bars, **kw)


def test_long_through_bar_fills_at_the_exact_stop_level():
    """The bar trades down through 95 without opening below it, so the resting
    stop fills AT its level — not at the open, not at the low."""
    rules = [StopLossRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(99.0, 99.5, 94.0, 96.0)]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (2, 95.0)
    assert (got.exit_rule_kind, got.exit_rule_index, got.entry_bar) == ("stop_loss", 0, 1)


def test_long_gap_through_bar_fills_at_the_worse_open():
    """The bar opens at 90, already below the 95 stop, so the fill is the open —
    a resting stop cannot fill at a level the market never offered."""
    rules = [StopLossRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(90.0, 91.0, 88.0, 89.0)]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (2, 90.0)


def test_short_through_bar_fills_at_the_exact_stop_level():
    rules = [StopLossRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 106.0, 100.5, 104.0)]
    got = _resolve(rules, bars, side="short")
    assert (got.exit_bar, got.exit_price) == (2, 105.0)


def test_short_gap_through_bar_fills_at_the_worse_open():
    rules = [StopLossRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(110.0, 112.0, 109.0, 111.0)]
    got = _resolve(rules, bars, side="short")
    assert (got.exit_bar, got.exit_price) == (2, 110.0)


def test_stop_is_not_eligible_on_its_own_entry_bar():
    """The resting order materializes at entry fill and is not eligible until
    the next bar — so a breach on the entry bar itself does not fire, and the
    stop only fills when a LATER bar breaches."""
    rules = [StopLossRule(pct=0.05)]
    breach_on_entry = _bar(100.0, 100.0, 90.0, 99.0)
    bars = [_flat(100.0), breach_on_entry, _flat(100.0)]
    assert _resolve(rules, bars) is None


def test_no_stop_rule_produces_no_exit():
    bars = [_flat(100.0), _flat(100.0), _bar(50.0, 50.0, 40.0, 45.0)]
    assert _resolve([TakeProfitRule(pct=0.1)], bars) is None


def test_side_incompatible_basis_never_fires():
    """``trailing_low`` is a short-side basis; on a long it is a no-op, not a
    same-bar flush."""
    rules = [StopLossRule(pct=0.05, basis="trailing_low")]
    bars = [_flat(100.0), _flat(100.0), _bar(50.0, 50.0, 40.0, 45.0)]
    assert _resolve(rules, bars) is None


def test_position_open_at_the_last_bar_produces_no_record():
    rules = [StopLossRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0)]
    assert _resolve(rules, bars) is None


def test_exit_date_comes_from_the_fill_bar():
    rules = [StopLossRule(pct=0.05)]
    bars = [
        _flat(100.0, "2024-03-01T00:00:00"),
        _flat(100.0, "2024-03-02T00:00:00"),
        _bar(99.0, 99.0, 90.0, 91.0, "2024-03-03T14:30:00"),
    ]
    assert _resolve(rules, bars).exit_date == "2024-03-03"


def test_first_breaching_bar_wins_not_a_later_one():
    rules = [StopLossRule(pct=0.05)]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(99.0, 99.0, 94.0, 96.0),
        _bar(96.0, 96.0, 80.0, 82.0),
    ]
    assert _resolve(rules, bars).exit_bar == 2


def test_lowest_spec_index_wins_when_two_stops_reach_on_one_bar():
    """Ties break by ascending spec index, matching the engine's spec-order
    walk — the looser stop at index 0 wins even though the tighter one at index
    1 is also breached."""
    rules = [StopLossRule(pct=0.10), StopLossRule(pct=0.03)]
    bars = [_flat(100.0), _flat(100.0), _bar(99.0, 99.0, 85.0, 86.0)]
    got = _resolve(rules, bars)
    assert (got.exit_rule_index, got.exit_price) == (0, 90.0)


def test_a_later_stop_still_fires_when_no_earlier_one_triggers():
    rules = [StopLossRule(pct=0.10), StopLossRule(pct=0.03)]
    bars = [_flat(100.0), _flat(100.0), _bar(99.0, 99.0, 96.0, 96.5)]
    got = _resolve(rules, bars)
    assert (got.exit_rule_index, got.exit_price) == (1, 97.0)


def test_exit_price_is_rounded_to_the_production_bucket():
    """A percentage-derived level carries more places than production stores, so
    an unrounded reference price would mismatch every trade."""
    rules = [StopLossRule(pct=0.0333)]
    bars = [_flat(9.0), _flat(9.0), _bar(8.9, 8.9, 8.0, 8.1)]
    got = _resolve(rules, bars)
    assert got.exit_price == 8.7003  # 9 * (1 - 0.0333) = 8.700300000000..., sub-$10 bucket


def test_out_of_range_entry_bar_is_rejected():
    with pytest.raises(ValueError, match="out of range"):
        resolve_stop_loss_exit([StopLossRule(pct=0.05)], _entry(entry_bar=9), [_flat(100.0)])


# ---------------------------------------------------------------------------
# Nonpositive / non-finite fill prices
# ---------------------------------------------------------------------------


def test_nonfinite_fill_price_is_skipped_and_a_later_bar_still_fires():
    """A degenerate bar suppresses one candidate fill rather than aborting the
    run or emitting an invalid record."""
    rules = [StopLossRule(pct=0.05)]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(float("nan"), 99.0, 90.0, 95.0),  # triggers, but open is NaN
        _bar(96.0, 96.0, 90.0, 91.0),
    ]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (3, 95.0)


def test_nonpositive_gap_open_is_skipped():
    rules = [StopLossRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(0.0, 99.0, -1.0, 50.0), _flat(100.0)]
    assert _resolve(rules, bars) is None


# ---------------------------------------------------------------------------
# Trailing bases
# ---------------------------------------------------------------------------


def test_trailing_high_ratchets_the_floor_up_as_price_rises():
    """The floor follows the running high: after a bar peaking at 120 the floor
    is 114, so a pullback to 113 stops out — a move that would not have touched
    the original 95 floor."""
    rules = [StopLossRule(pct=0.05, basis="trailing_high")]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 120.0, 100.0, 119.0),  # sets the watermark to 120
        _bar(119.0, 119.0, 113.0, 114.0),  # 113 <= 114 floor -> fires
    ]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (3, 114.0)


def test_trailing_low_ratchets_the_cap_down_for_a_short():
    rules = [StopLossRule(pct=0.05, basis="trailing_low")]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 100.0, 80.0, 81.0),  # watermark down to 80
        _bar(81.0, 85.0, 81.0, 84.0),  # cap is 84 -> 85 >= 84 fires
    ]
    got = _resolve(rules, bars, side="short")
    assert (got.exit_bar, got.exit_price) == (3, 84.0)


def test_trailing_watermark_is_evaluated_before_it_is_extended():
    """The single most load-bearing ordering rule. This bar's own high must NOT
    raise the floor that this bar's low is then tested against — otherwise an
    ordinary wide bar reads as a stop-out.

    Long, anchor 100, 5% trail. The bar runs 112..120: folding the high in first
    would set the floor to 114 and stop out at ~101 on a bar that closed up 18%.
    Evaluating first tests against the 95 floor, which 112 never breaches.
    """
    rules = [StopLossRule(pct=0.05, basis="trailing_high")]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 120.0, 112.0, 118.0)]
    assert _resolve(rules, bars) is None


def test_trailing_watermark_seeds_at_the_anchor_not_the_entry_bars_high():
    """The entry bar's range never enters the watermark: the order materializes
    at entry fill, seeded from that fill price. A 112 spike on the entry bar
    would otherwise put the floor at 106.4 and stop the position out
    immediately on the next ordinary bar."""
    rules = [StopLossRule(pct=0.05, basis="trailing_high")]
    bars = [
        _flat(100.0),
        _bar(100.0, 112.0, 99.5, 101.0),  # entry bar spikes to 112
        _bar(101.0, 102.0, 100.0, 101.0),  # 100 > 95 floor -> no fire
    ]
    assert _resolve(rules, bars) is None


def test_trailing_stop_gap_through_fills_at_the_worse_open():
    rules = [StopLossRule(pct=0.05, basis="trailing_high")]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 120.0, 100.0, 119.0),  # floor becomes 114
        _bar(105.0, 106.0, 104.0, 105.0),  # opens below the floor
    ]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (3, 105.0)


def test_trailing_floor_never_ratchets_down():
    """A pullback bar must not lower the floor; only favorable moves move it."""
    rules = [StopLossRule(pct=0.05, basis="trailing_high")]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 120.0, 100.0, 119.0),  # watermark 120, floor 114
        _bar(119.0, 119.0, 115.0, 116.0),  # pullback, no new high, no fire
        _bar(116.0, 116.0, 113.9, 114.0),  # floor is still 114 -> fires
    ]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (4, 114.0)


# ---------------------------------------------------------------------------
# style="limit"
# ---------------------------------------------------------------------------


def _limit_rule(pct: float = 0.05, offset: float = 0.02) -> StopLossRule:
    return StopLossRule(pct=pct, style="limit", limit_offset_pct=offset)


def test_limit_stop_fills_at_exactly_the_limit_price():
    """Stop at 95, limit 2% below it at 93.1. The bar reaches down through the
    stop and back up over the limit, so it fills AT the limit — never at the
    stop, and never gap-adjusted worse."""
    rules = [_limit_rule()]
    bars = [_flat(100.0), _flat(100.0), _bar(99.0, 99.0, 92.0, 94.0)]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (2, 93.1)


def test_limit_stop_gap_through_does_not_fill_and_leaves_the_position_open():
    """The defining stop-limit trade-off: the bar's ENTIRE range sits below the
    93.1 limit, so there is no fill and the position stays open."""
    rules = [_limit_rule()]
    bars = [_flat(100.0), _flat(100.0), _bar(90.0, 92.0, 88.0, 89.0)]
    assert _resolve(rules, bars) is None


def test_limit_stop_stays_armed_and_fills_on_a_later_recovery_bar():
    """Once the stop level is crossed the order latches: it must not require the
    stop to be re-crossed, or a gap-through would leave it stuck open forever."""
    rules = [_limit_rule()]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(90.0, 92.0, 88.0, 89.0),  # arms, gaps through, no fill
        _bar(89.0, 94.0, 89.0, 93.5),  # recovers over 93.1 -> fills
    ]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (3, 93.1)


def test_limit_stop_reachability_is_judged_on_the_range_not_the_open():
    """A bar that OPENS below the limit but trades back up to it still fills."""
    rules = [_limit_rule()]
    bars = [_flat(100.0), _flat(100.0), _bar(91.0, 93.5, 90.0, 93.0)]
    got = _resolve(rules, bars)
    assert (got.exit_bar, got.exit_price) == (2, 93.1)


def test_limit_stop_does_not_fill_before_its_stop_level_is_breached():
    """The limit sits below the stop, so a bar hovering above the stop must not
    fill just because the limit is technically 'reachable' from above."""
    rules = [_limit_rule()]
    bars = [_flat(100.0), _flat(100.0), _bar(99.0, 99.5, 96.0, 97.0)]
    assert _resolve(rules, bars) is None


def test_short_limit_stop_places_its_limit_above_the_stop():
    """Closing a short is a buy, so the protective limit sits ABOVE the stop:
    stop 105, limit 105 * 1.02 = 107.1."""
    rules = [_limit_rule()]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 108.0, 101.0, 107.0)]
    got = _resolve(rules, bars, side="short")
    assert (got.exit_bar, got.exit_price) == (2, 107.1)


def test_short_limit_stop_gap_through_does_not_fill():
    rules = [_limit_rule()]
    bars = [_flat(100.0), _flat(100.0), _bar(110.0, 112.0, 108.0, 111.0)]
    assert _resolve(rules, bars, side="short") is None


# ---------------------------------------------------------------------------
# Slippage anchoring end-to-end
# ---------------------------------------------------------------------------


def test_slippage_shifts_the_stop_level_and_the_recorded_price():
    """A long fills 200bps worse at 102, so its 5% stop sits at 96.9, not 95."""
    rules = [StopLossRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(99.0, 99.0, 94.0, 95.0)]
    assert _resolve(rules, bars, entry_slippage_bps=0.0).exit_price == 95.0
    assert _resolve(rules, bars, entry_slippage_bps=200.0).exit_price == 96.9


def test_slippage_can_change_which_bar_the_stop_fires_on():
    rules = [StopLossRule(pct=0.05)]
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(98.0, 98.0, 96.5, 97.0),  # below 96.9 but above 95
        _bar(97.0, 97.0, 94.0, 94.5),
    ]
    assert _resolve(rules, bars, entry_slippage_bps=0.0).exit_bar == 3
    assert _resolve(rules, bars, entry_slippage_bps=200.0).exit_bar == 2


# ---------------------------------------------------------------------------
# replay_stop_loss_exits — the (spec, bars) entry point
# ---------------------------------------------------------------------------


def test_replay_opens_from_entry_rules_and_closes_on_the_stop():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    bars = {
        "AAA": [
            _flat(101.0),  # entry predicate fires (close > 100)
            _bar(100.0, 100.0, 100.0, 100.0),  # entry fills here at open 100
            _bar(99.0, 99.0, 94.0, 96.0),  # breaches the 95 stop
        ]
    }
    (got,) = replay_stop_loss_exits(spec, bars)
    assert (got.symbol, got.entry_bar, got.exit_bar, got.exit_price) == ("AAA", 1, 2, 95.0)


def test_replay_returns_nothing_when_no_entry_fires():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    bars = {"AAA": [_flat(50.0), _flat(50.0), _bar(50.0, 50.0, 10.0, 20.0)]}
    assert replay_stop_loss_exits(spec, bars) == []


def test_replay_returns_nothing_when_the_position_never_stops_out():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _flat(101.0), _flat(102.0)]}
    assert replay_stop_loss_exits(spec, bars) == []


def test_replay_handles_symbols_independently():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    stops_out = [_flat(101.0), _flat(100.0), _bar(99.0, 99.0, 94.0, 96.0)]
    never_stops = [_flat(101.0), _flat(100.0), _flat(101.0)]
    got = replay_stop_loss_exits(spec, {"AAA": stops_out, "BBB": never_stops})
    assert [r.symbol for r in got] == ["AAA"]


def test_replay_fires_the_injected_short_safety_stop():
    """A short with no authored stop still closes when price doubles against it,
    attributed to the injected rule at index ``len(spec.exit_rules)``."""
    spec = _spec(exit_rules=[], entry_side="short")
    bars = {"AAA": [_flat(101.0), _flat(100.0), _bar(150.0, 210.0, 150.0, 205.0)]}
    (got,) = replay_stop_loss_exits(spec, bars)
    assert (got.exit_rule_index, got.exit_price) == (0, 200.0)


def test_replay_passes_slippage_through_to_the_anchor():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _bar(99.0, 99.0, 94.0, 96.0)]}
    assert replay_stop_loss_exits(spec, bars, entry_slippage_bps=200.0)[0].exit_price == 96.9


@pytest.mark.parametrize(
    ("exit_rules", "case"),
    [
        ([StopLossRule(pct=0.05)], "authored stop, nothing injected"),
        ([], "no short stop, so the safety stop IS injected"),
    ],
    ids=["authored_stop", "injected_safety_stop"],
)
def test_replay_does_not_mutate_its_inputs(exit_rules, case):
    """Both branches of the injection, since only one of them appends.

    The injecting branch is where mutation is actually plausible — it is the
    only path that grows a rule list — so checking only the authored-stop spec
    would leave the risky case unverified.
    """
    spec = _spec(exit_rules=exit_rules, entry_side="short")
    bars = {"AAA": [_flat(101.0), _flat(100.0), _bar(99.0, 210.0, 99.0, 205.0)]}
    exit_rules_before = list(spec.exit_rules)
    bars_before = {k: list(v) for k, v in bars.items()}
    replay_stop_loss_exits(spec, bars)
    assert list(spec.exit_rules) == exit_rules_before, case
    assert {k: list(v) for k, v in bars.items()} == bars_before, case


def test_replay_is_deterministic():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _bar(99.0, 99.0, 94.0, 96.0)]}
    assert replay_stop_loss_exits(spec, bars) == replay_stop_loss_exits(spec, bars)


def test_replay_respects_target_symbol_gating():
    spec = _spec(exit_rules=[StopLossRule(pct=0.05)], target_symbols=["AAA"])
    series = [_flat(101.0), _flat(100.0), _bar(99.0, 99.0, 94.0, 96.0)]
    got = replay_stop_loss_exits(spec, {"AAA": list(series), "ZZZ": list(series)})
    assert [r.symbol for r in got] == ["AAA"]


# ---------------------------------------------------------------------------
# ReferenceStopLossExit value-object contract
# ---------------------------------------------------------------------------


def _record(**overrides) -> ReferenceStopLossExit:
    kwargs = {
        "symbol": "AAA",
        "entry_bar": 1,
        "exit_bar": 4,
        "exit_date": "2024-01-05",
        "exit_price": 95.0,
        "exit_rule_kind": "stop_loss",
        "exit_rule_index": 0,
    }
    kwargs.update(overrides)
    return ReferenceStopLossExit(**kwargs)


def test_valid_record_constructs():
    assert _record().exit_price == 95.0


def test_negative_entry_bar_is_rejected():
    with pytest.raises(ValueError, match="entry_bar"):
        _record(entry_bar=-1)


@pytest.mark.parametrize("exit_bar", [0, 1])
def test_exit_bar_must_be_strictly_after_entry_bar(exit_bar):
    """Strict: no modeled exit can complete on the entry bar itself, since a
    resting order is not eligible until the bar after it materializes."""
    with pytest.raises(ValueError, match="exit_bar"):
        _record(entry_bar=1, exit_bar=exit_bar)


def test_negative_exit_rule_index_is_rejected():
    with pytest.raises(ValueError, match="exit_rule_index"):
        _record(exit_rule_index=-1)


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_nonpositive_or_nonfinite_exit_price_is_rejected(price):
    with pytest.raises(ValueError, match="exit_price"):
        _record(exit_price=price)


def test_wrong_exit_rule_kind_is_rejected():
    with pytest.raises(ValueError, match="exit_rule_kind"):
        _record(exit_rule_kind="take_profit")


def test_record_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _record().exit_price = 1.0


def test_record_carries_no_level_index():
    """``level_index`` is meaningful only for a scaled-take-profit close; a
    field that could only ever be ``None`` here would carry no information."""
    assert not hasattr(_record(), "level_index")


# ---------------------------------------------------------------------------
# Watermark parity against the existing independent implementation
# ---------------------------------------------------------------------------


def _gate_replay_first_trigger(rule: StopLossRule, anchor: float, bars, side: str):
    """The conformance gate's trailing-watermark reconstruction, inlined.

    The gate's own ``_check_stop_loss_trailing_replay`` is driven by
    ``TradeRecord``\\ s and imports ``trading_service.service``, so it cannot be
    called directly against bar fixtures. What is reproduced here is its
    watermark loop verbatim in shape — seed at the entry price, ask the SHARED
    ``stop_loss_triggers`` geometry, then extend after the check — which is the
    property the design doc requires this module be pinned against.
    """
    from investment_team.strategy_lab.executor.rule_compiler import (
        BarSnapshot,
        PositionState,
        stop_loss_triggers,
    )

    hi = lo = anchor
    for i, b in enumerate(bars):
        position = PositionState(
            symbol="AAA",
            side=side,
            qty=1.0,
            entry_price=anchor,
            high_since_entry=hi,
            low_since_entry=lo,
        )
        if stop_loss_triggers(rule, position, BarSnapshot(high=b.high, low=b.low, close=b.close)):
            return i
        hi = max(hi, b.high)
        lo = min(lo, b.low)
    return None


@pytest.mark.parametrize(
    "series",
    [
        [(100.0, 105.0, 99.0, 104.0), (104.0, 110.0, 103.0, 109.0), (109.0, 109.0, 100.0, 101.0)],
        [(100.0, 120.0, 100.0, 119.0), (119.0, 119.0, 113.0, 114.0)],
        [(100.0, 101.0, 99.0, 100.0), (100.0, 100.0, 94.0, 95.0)],
        [(100.0, 130.0, 100.0, 129.0), (129.0, 140.0, 128.0, 139.0), (139.0, 139.0, 130.0, 131.0)],
    ],
)
def test_trailing_ratchet_matches_the_conformance_gate_replay(series):
    """Design-doc parity mandate: this module's watermark ratchet must agree
    with the pre-existing independent reconstruction on which bar first
    triggers.

    Fixtures deliberately start AFTER the entry bar, because the two
    implementations differ on exactly one axis — whether the entry bar's own
    range enters the watermark — which the next test pins separately.
    """
    rule = StopLossRule(pct=0.05, basis="trailing_high")
    post_entry = [_bar(*ohlc) for ohlc in series]
    bars = [_flat(100.0), _flat(100.0), *post_entry]

    got = _resolve([rule], bars)
    mine = None if got is None else got.exit_bar - 2
    assert mine == _gate_replay_first_trigger(rule, 100.0, post_entry, "long")


def test_entry_bar_range_is_the_one_intended_difference_from_the_gate_replay():
    """The gate replays a market entry from the entry bar itself, folding that
    bar's high into the watermark; this module models the target resting-order
    lifecycle, where the order is seeded from the entry fill and is not eligible
    until the next bar. That is a deliberate divergence, recorded here rather
    than left to be discovered as a mystery.
    """
    rule = StopLossRule(pct=0.05, basis="trailing_high")
    entry_bar = _bar(100.0, 112.0, 99.5, 101.0)  # spikes to 112
    next_bar = _bar(101.0, 102.0, 100.0, 101.0)

    # Gate-style: entry bar folded in -> floor 106.4 -> the next bar's 100 fires.
    assert _gate_replay_first_trigger(rule, 100.0, [entry_bar, next_bar], "long") == 1
    # This module: watermark seeded at the anchor -> floor 95 -> no fire.
    assert _resolve([rule], [_flat(100.0), entry_bar, next_bar]) is None


def test_module_imports_no_forbidden_engine_module():
    """The design doc's module boundary, asserted rather than left to prose:
    importing this module must not drag in the live engine.

    Run in a subprocess because the boundary is about what the IMPORT does — in
    this process the forbidden modules are already loaded by other tests, so
    checking ``sys.modules`` here would prove nothing.

    The module name and ``sys.path`` are taken from the running interpreter
    rather than hardcoded: the package root differs between a run rooted at
    ``backend/`` (``agents.investment_team...``) and one rooted at
    ``backend/agents/`` (``investment_team...``), and CI uses the latter.
    Forbidden modules are matched by suffix for the same reason.
    """
    import subprocess
    import sys

    from investment_team.strategy_lab.executor import reference_exits

    code = (
        "import sys\n"
        f"sys.path[:] = {list(sys.path)!r}\n"
        f"import {reference_exits.__name__}\n"
        "hits = [\n"
        "    m\n"
        "    for m in sys.modules\n"
        "    if m.endswith('trading_service.service')\n"
        "    or m.endswith('trading_service.engine')\n"
        "    or '.trading_service.engine.' in m\n"
        "]\n"
        "print(sorted(hits))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "[]", proc.stdout


def test_math_helper_is_used_for_the_finiteness_guard():
    """Guard against a future refactor swapping the finiteness check for a bare
    ``> 0``, which silently admits ``+inf``."""
    assert not math.isfinite(float("inf"))
    with pytest.raises(ValueError):
        _record(exit_price=float("inf"))


def test_fill_qty_rel_tol_is_a_local_constant_not_imported_from_order_book():
    """Guards the deliberate duplication of ``FILL_QTY_REL_TOL``: the constant
    must be defined locally, never imported from the forbidden ``order_book``
    module. A value-only assertion would not catch a future contributor
    replacing the local definition with an import of the same-valued
    constant, so this also parses the module's own AST and checks no
    ``import``/``from ... import`` statement names ``order_book`` — narrower
    than a source-text substring check, which would also trip on this
    module's own docstring prose describing the forbidden-module list.
    """
    import ast
    import inspect

    from investment_team.strategy_lab.executor import reference_exits

    assert reference_exits._FILL_QTY_REL_TOL == 1e-12
    tree = ast.parse(inspect.getsource(reference_exits))
    import_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                import_names.append(node.module)
            # ``from <package> import order_book`` / ``from . import
            # order_book`` name the forbidden module as the IMPORTED alias,
            # not the ``module`` (the package it's imported from) — this
            # codebase's own dominant import idiom (see this file's own
            # imports above), so the alias names must be checked too.
            import_names.extend(alias.name for alias in node.names)
    assert not any("order_book" in name for name in import_names)


def test_fill_qty_rel_tol_value_matches_production_source():
    """Guards against silent drift: the local mirror's VALUE, not just its
    provenance (the test above), must still match production's constant.
    Reads ``order_book.py`` as TEXT rather than importing it, so this stays
    inside the module's own forbidden-import boundary while still failing
    loudly if a future production change moves the tolerance and this
    reference simulator's close semantics silently diverge from it."""
    import re
    from pathlib import Path

    from investment_team.strategy_lab.executor.reference_exits import _FILL_QTY_REL_TOL

    order_book_path = (
        Path(__file__).resolve().parents[1] / "trading_service" / "engine" / "order_book.py"
    )
    source = order_book_path.read_text()
    match = re.search(r"^FILL_QTY_REL_TOL\s*=\s*([0-9eE.+-]+)", source, re.MULTILINE)
    assert match, "FILL_QTY_REL_TOL definition not found in order_book.py"
    assert float(match.group(1)) == _FILL_QTY_REL_TOL


# ---------------------------------------------------------------------------
# take_profit_rules / scaled_take_profit_rules — filtering helpers
# ---------------------------------------------------------------------------


def test_take_profit_rules_returns_only_take_profit_rule_instances_in_spec_order():
    rules = [
        StopLossRule(pct=0.05),
        TakeProfitRule(pct=0.1),
        ScaledTakeProfitRule(levels=[TakeProfitLevel(pct=0.05, qty_fraction=1.0)]),
        TakeProfitRule(pct=0.2),
    ]
    assert [i for i, _ in take_profit_rules(rules)] == [1, 3]


def test_scaled_take_profit_rules_returns_only_scaled_take_profit_rule_instances_in_spec_order():
    rules = [
        StopLossRule(pct=0.05),
        TakeProfitRule(pct=0.1),
        ScaledTakeProfitRule(levels=[TakeProfitLevel(pct=0.05, qty_fraction=1.0)]),
        TakeProfitRule(pct=0.2),
    ]
    assert [i for i, _ in scaled_take_profit_rules(rules)] == [2]


# ---------------------------------------------------------------------------
# Standalone take_profit — exact-price fill geometry
# ---------------------------------------------------------------------------


def _resolve_tp(rules, bars, side="long", entry_bar=1, **kw):
    return resolve_take_profit_family_exit(
        rules, _entry(side=side, entry_bar=entry_bar), bars, **kw
    )


def test_long_take_profit_fills_at_the_exact_target_through_bar():
    """The bar trades up through 105 without gapping past it, so the resting
    target fills AT the target — not at the open, not at the high."""
    rules = [TakeProfitRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.5)]
    got = _resolve_tp(rules, bars)
    assert (got.exit_bar, got.exit_price) == (2, 105.0)
    assert (got.exit_rule_kind, got.exit_rule_index, got.entry_bar) == ("take_profit", 0, 1)
    assert got.level_index is None


def test_long_take_profit_gap_through_still_fills_at_exactly_the_target():
    """The bar opens at 110, already above the 105 target, but a limit fills
    exactly at its price — never better, never worse — unlike a stop."""
    rules = [TakeProfitRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(110.0, 112.0, 109.0, 111.0)]
    got = _resolve_tp(rules, bars)
    assert (got.exit_bar, got.exit_price) == (2, 105.0)


def test_short_take_profit_fills_at_the_exact_target_through_bar():
    rules = [TakeProfitRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(99.0, 99.5, 94.0, 96.0)]
    got = _resolve_tp(rules, bars, side="short")
    assert (got.exit_bar, got.exit_price) == (2, 95.0)


def test_short_take_profit_gap_through_still_fills_at_exactly_the_target():
    rules = [TakeProfitRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _bar(90.0, 91.0, 88.0, 89.0)]
    got = _resolve_tp(rules, bars, side="short")
    assert (got.exit_bar, got.exit_price) == (2, 95.0)


def test_take_profit_is_not_eligible_on_its_own_entry_bar():
    """The resting order materializes at entry fill and is not eligible until
    the next bar — a target reached on the entry bar itself does not fire."""
    rules = [TakeProfitRule(pct=0.05)]
    breach_on_entry = _bar(100.0, 106.0, 100.0, 105.0)
    bars = [_flat(100.0), breach_on_entry, _flat(100.0)]
    assert _resolve_tp(rules, bars) is None


def test_no_take_profit_rule_produces_no_exit():
    bars = [_flat(100.0), _flat(100.0), _bar(150.0, 160.0, 150.0, 155.0)]
    assert _resolve_tp([StopLossRule(pct=0.1)], bars) is None


def test_position_open_at_the_last_bar_produces_no_take_profit_record():
    rules = [TakeProfitRule(pct=0.05)]
    bars = [_flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0)]
    assert _resolve_tp(rules, bars) is None


def test_take_profit_exit_date_comes_from_the_fill_bar():
    rules = [TakeProfitRule(pct=0.05)]
    bars = [
        _flat(100.0, "2024-03-01T00:00:00"),
        _flat(100.0, "2024-03-02T00:00:00"),
        _bar(101.0, 106.0, 100.0, 105.0, "2024-03-03T14:30:00"),
    ]
    assert _resolve_tp(rules, bars).exit_date == "2024-03-03"


def test_take_profit_exit_price_is_rounded_to_the_production_bucket():
    """A percentage-derived target carries more places than production stores,
    so an unrounded reference price would mismatch every trade."""
    rules = [TakeProfitRule(pct=0.0333)]
    bars = [_flat(9.0), _flat(9.0), _bar(8.9, 9.4, 8.9, 9.3)]
    got = _resolve_tp(rules, bars)
    assert got.exit_price == pytest.approx(9.2997, abs=1e-9)  # 9 * 1.0333, sub-$10 bucket


def test_out_of_range_entry_bar_is_rejected_for_take_profit():
    with pytest.raises(ValueError, match="out of range"):
        resolve_take_profit_family_exit(
            [TakeProfitRule(pct=0.05)], _entry(entry_bar=9), [_flat(100.0)]
        )


# ---------------------------------------------------------------------------
# Standalone-vs-standalone tie-breaking
# ---------------------------------------------------------------------------


def test_lowest_spec_index_standalone_take_profit_wins_when_two_targets_reach_on_one_bar():
    rules = [TakeProfitRule(pct=0.10), TakeProfitRule(pct=0.03)]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 115.0, 100.0, 112.0)]
    got = _resolve_tp(rules, bars)
    assert (got.exit_rule_index, got.exit_price) == (0, 110.0)


def test_a_later_standalone_take_profit_still_fires_when_no_earlier_one_triggers():
    rules = [TakeProfitRule(pct=0.10), TakeProfitRule(pct=0.03)]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 104.0, 100.0, 103.5)]
    got = _resolve_tp(rules, bars)
    assert (got.exit_rule_index, got.exit_price) == (1, 103.0)


# ---------------------------------------------------------------------------
# Single-ladder sequential rungs
# ---------------------------------------------------------------------------


def test_single_ladder_first_rung_closes_the_configured_fraction_and_leaves_position_open():
    """A ladder whose only rung so far reduces, but does not empty, the
    position produces no record while bars remain to check the next rung."""
    rule = ScaledTakeProfitRule(
        levels=[
            TakeProfitLevel(pct=0.05, qty_fraction=0.5),
            TakeProfitLevel(pct=0.10, qty_fraction=0.5),
        ]
    )
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.0)]
    assert _resolve_tp([rule], bars) is None


def test_single_ladder_multi_rung_sequential_bars_produce_correct_weighted_average_exit_price():
    rule = ScaledTakeProfitRule(
        levels=[
            TakeProfitLevel(pct=0.05, qty_fraction=0.5),
            TakeProfitLevel(pct=0.10, qty_fraction=0.5),
        ]
    )
    bars = [
        _flat(100.0),
        _flat(100.0),  # 1: entry
        _bar(101.0, 106.0, 100.0, 105.0),  # 2: rung 0 fires at 105, qty 0.5
        _flat(105.0),  # 3: filler, rung 1 (110) not reached
        _bar(109.0, 111.0, 108.0, 110.0),  # 4: rung 1 fires at 110, qty 0.5 -> closes
    ]
    got = _resolve_tp([rule], bars)
    assert (got.exit_bar, got.exit_rule_kind, got.exit_rule_index, got.level_index) == (
        4,
        "scaled_take_profit",
        0,
        1,
    )
    assert got.exit_price == pytest.approx(107.5)  # (0.5*105 + 0.5*110) / 1.0


def test_ladder_that_sums_to_exactly_one_fully_closes_with_the_last_rungs_level_index():
    rule = ScaledTakeProfitRule(
        levels=[
            TakeProfitLevel(pct=0.02, qty_fraction=0.2),
            TakeProfitLevel(pct=0.05, qty_fraction=0.3),
            TakeProfitLevel(pct=0.10, qty_fraction=0.5),
        ]
    )
    bars = [
        _flat(100.0),
        _flat(100.0),  # 1: entry
        _bar(101.0, 103.0, 100.0, 102.0),  # 2: rung 0 (102) fires, qty 0.2
        _bar(102.0, 106.0, 101.0, 105.0),  # 3: rung 1 (105) fires, qty 0.3
        _bar(105.0, 112.0, 104.0, 111.0),  # 4: rung 2 (110) fires, qty 0.5 -> closes
    ]
    got = _resolve_tp([rule], bars)
    assert (got.exit_bar, got.level_index) == (4, 2)
    assert got.exit_price == pytest.approx(106.9)  # 0.2*102 + 0.3*105 + 0.5*110


def test_ladder_rounding_bucket_comes_from_the_terminal_rung_not_the_blended_average():
    """The rung prices straddle the $10 bucket boundary: the blended average
    (9.675) is itself under $10, but the FINAL closing rung (11.7) is not.
    Production derives the rounding bucket from the terminal slice's own
    price, then rounds the blended average with it — so the correct result
    is 9.68 (2dp, from the terminal rung), not 9.675 (4dp, from the blend
    itself, which is what a bucket-from-the-blended-value bug would produce).
    """
    rule = ScaledTakeProfitRule(
        levels=[
            TakeProfitLevel(pct=0.05, qty_fraction=0.9),  # target 9.45
            TakeProfitLevel(pct=0.30, qty_fraction=0.1),  # target 11.7 (terminal, >= $10)
        ]
    )
    bars = [
        _flat(9.0),
        _flat(9.0),  # 1: entry, anchor 9.0
        _bar(9.1, 9.5, 9.0, 9.4),  # 2: rung 0 (9.45) fires, qty 0.9
        _bar(9.4, 11.8, 9.3, 11.5),  # 3: rung 1 (11.7) fires, qty 0.1 -> closes
    ]
    got = _resolve_tp([rule], bars)
    assert got.exit_bar == 3
    assert got.exit_price == 9.68  # NOT 9.675 (4dp bucket from the blended value)


def test_ladder_that_sums_to_less_than_one_leaves_a_residual_open_and_emits_no_record():
    rule = ScaledTakeProfitRule(levels=[TakeProfitLevel(pct=0.05, qty_fraction=0.5)])
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(101.0, 106.0, 100.0, 105.0),  # only rung fires, 0.5 remains open forever
        _flat(105.0),
        _flat(105.0),
    ]
    assert _resolve_tp([rule], bars) is None


def test_ladder_shortfall_within_fill_qty_rel_tol_still_closes_and_emits_a_record():
    """Floating-point summation noise (e.g. rungs landing at ``1.0 - 1e-13``
    instead of exactly ``1.0``) must still be treated as fully closed — this
    exercises ``_FILL_QTY_REL_TOL`` (1e-12), not the DSL's ``LADDER_SUM_TOL``
    (1e-9, a validator bound on the sum, unrelated to this runtime check)."""
    rule = ScaledTakeProfitRule(levels=[TakeProfitLevel(pct=0.05, qty_fraction=1.0 - 1e-13)])
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.0)]
    got = _resolve_tp([rule], bars)
    assert got is not None
    assert (got.exit_bar, got.exit_rule_kind, got.level_index) == (2, "scaled_take_profit", 0)
    assert got.exit_price == pytest.approx(105.0)


# ---------------------------------------------------------------------------
# One-rung-per-bar-per-position enforcement
# ---------------------------------------------------------------------------


def test_a_spike_bar_that_crosses_two_rungs_fires_only_the_cursor_rung_this_bar():
    """A bar whose range clears both rungs at once still only advances the
    cursor by one; the second rung fires on a later bar, matching the shipped
    engine's ``_ScaledLadderCursor`` (never the acceptance-criteria wording of
    "all crossed rungs fill in one bar" — see the plan's resolved ambiguity)."""
    rule = ScaledTakeProfitRule(
        levels=[
            TakeProfitLevel(pct=0.05, qty_fraction=0.3),
            TakeProfitLevel(pct=0.10, qty_fraction=0.7),
        ]
    )
    bars = [
        _flat(100.0),
        _flat(100.0),  # 1: entry
        _bar(101.0, 115.0, 100.0, 112.0),  # 2: high=115 clears BOTH 105 and 110
        _bar(110.0, 111.0, 109.0, 110.5),  # 3: rung 1 (110) fires here, not bar 2
    ]
    got = _resolve_tp([rule], bars)
    assert got.exit_bar == 3  # not 2 — proves only the cursor rung fired on the spike bar
    assert got.level_index == 1
    assert got.exit_price == pytest.approx(108.5)  # 0.3*105 + 0.7*110


def test_a_rung_does_not_fire_on_a_bar_whose_own_range_never_reaches_it_even_after_an_earlier_spike():
    """Production's ``_next_scaled_rung`` is watermark-based: a rung a single
    spike bar clears stays "eligible" on every later bar even after price
    retraces. This module must NOT reproduce that — a rung only fires on a
    bar whose OWN range reaches its target, so a fabricated fill on a bar that
    never traded there must not occur."""
    rule = ScaledTakeProfitRule(
        levels=[
            TakeProfitLevel(pct=0.02, qty_fraction=0.3),
            TakeProfitLevel(pct=0.08, qty_fraction=0.7),
        ]
    )
    bars = [
        _flat(100.0),
        _flat(100.0),  # 1: entry
        # rung 0's target (102) AND rung 1's target (108) are both cleared by
        # this bar's high (109) — but only rung 0 (the cursor) is checked.
        _bar(101.0, 109.0, 100.0, 103.0),  # 2: rung 0 fires at 102
        _bar(103.0, 104.0, 102.0, 103.5),  # 3: rung 1 cursor, but high=104 < 108 -> no fire
        _flat(103.5),  # 4: still no fire
        _bar(107.0, 109.0, 106.0, 108.5),  # 5: high=109 >= 108 -> NOW fires
    ]
    got = _resolve_tp([rule], bars)
    assert got.exit_bar == 5
    assert got.level_index == 1
    assert got.exit_price == pytest.approx(106.2)  # 0.3*102 + 0.7*108


# ---------------------------------------------------------------------------
# Standalone-vs-rung tie-break, both directions
# ---------------------------------------------------------------------------


def test_standalone_take_profit_beats_a_reachable_ladder_rung_on_the_same_bar_when_it_has_the_lower_spec_index():
    rules = [
        TakeProfitRule(pct=0.05),  # index 0, target 105
        ScaledTakeProfitRule(
            levels=[TakeProfitLevel(pct=0.03, qty_fraction=1.0)]
        ),  # index 1, target 103
    ]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.0)]
    got = _resolve_tp(rules, bars)
    assert (got.exit_rule_kind, got.exit_rule_index, got.exit_price, got.level_index) == (
        "take_profit",
        0,
        105.0,
        None,
    )


def test_ladder_rung_beats_a_reachable_standalone_take_profit_on_the_same_bar_when_it_has_the_lower_spec_index():
    rules = [
        ScaledTakeProfitRule(
            levels=[TakeProfitLevel(pct=0.03, qty_fraction=1.0)]
        ),  # index 0, target 103
        TakeProfitRule(pct=0.05),  # index 1, target 105
    ]
    bars = [_flat(100.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.0)]
    got = _resolve_tp(rules, bars)
    assert (got.exit_rule_kind, got.exit_rule_index, got.exit_price, got.level_index) == (
        "scaled_take_profit",
        0,
        103.0,
        0,
    )


# ---------------------------------------------------------------------------
# Two ladders on one position — per-position (not per-ladder) firing budget
# ---------------------------------------------------------------------------


def test_two_ladders_on_one_position_fire_at_most_one_rung_per_bar_even_when_both_are_reachable():
    rules = [
        ScaledTakeProfitRule(
            levels=[TakeProfitLevel(pct=0.02, qty_fraction=0.5)]
        ),  # index 0, target 102
        ScaledTakeProfitRule(
            levels=[TakeProfitLevel(pct=0.06, qty_fraction=0.5)]
        ),  # index 1, target 106
    ]
    bars = [
        _flat(100.0),
        _flat(100.0),  # 1: entry
        # Both ladders' single rung is reachable this bar (high=108 >= 102 and
        # >= 106). If both fired here (a bug), the position would close on
        # THIS bar; correctly, only ladder 0 (lower index) fires here.
        _bar(101.0, 108.0, 100.0, 107.0),  # 2
        _bar(107.0, 109.0, 106.0, 108.0),  # 3: ladder 1's rung fires here instead
    ]
    got = _resolve_tp(rules, bars)
    assert got.exit_bar == 3  # not 2 — proves the two ladders did not both fire on bar 2
    assert got.exit_price == pytest.approx(104.0)  # 0.5*102 + 0.5*106


# ---------------------------------------------------------------------------
# _RestingTakeProfitFamily.step — re-invocation after a full close
# ---------------------------------------------------------------------------


def test_step_returns_none_on_any_call_after_the_position_has_fully_closed():
    """White-box test of the already-closed guard: ``resolve_take_profit_family_exit``
    itself never calls ``step`` again after a non-None result, so this exercises
    the contract a future direct caller (the docstring's anticipated combined
    multi-kind simulator) would rely on instead."""
    from investment_team.strategy_lab.executor.reference_exits import (
        _RestingTakeProfitFamily,
    )

    rules = [TakeProfitRule(pct=0.05)]
    family = _RestingTakeProfitFamily(side="long", symbol="AAA", anchor=100.0, rules=rules)
    closing_bar = _bar(101.0, 106.0, 100.0, 105.0)
    assert family.step(closing_bar) is not None
    # A second bar that would ALSO reach the target if evaluated fresh proves
    # the guard suppresses re-evaluation, not merely that nothing fired.
    also_reaches_target = _bar(101.0, 106.0, 100.0, 105.0)
    assert family.step(also_reaches_target) is None


# ---------------------------------------------------------------------------
# ReferenceTakeProfitExit value-object contract
# ---------------------------------------------------------------------------


def _record_tp(**overrides) -> ReferenceTakeProfitExit:
    """Build a valid standalone take-profit record; ``overrides`` replace any
    field so each validation test below violates exactly one invariant."""
    kwargs = {
        "symbol": "AAA",
        "entry_bar": 1,
        "exit_bar": 4,
        "exit_date": "2024-01-05",
        "exit_price": 105.0,
        "exit_rule_kind": "take_profit",
        "exit_rule_index": 0,
        "level_index": None,
    }
    kwargs.update(overrides)
    return ReferenceTakeProfitExit(**kwargs)


def test_valid_take_profit_record_constructs():
    assert _record_tp().exit_price == 105.0


def test_valid_scaled_take_profit_record_constructs():
    got = _record_tp(exit_rule_kind="scaled_take_profit", level_index=1)
    assert got.level_index == 1


def test_take_profit_record_rejects_a_level_index():
    with pytest.raises(ValueError, match="level_index"):
        _record_tp(exit_rule_kind="take_profit", level_index=0)


def test_scaled_take_profit_record_requires_a_level_index():
    with pytest.raises(ValueError, match="level_index"):
        _record_tp(exit_rule_kind="scaled_take_profit", level_index=None)


def test_scaled_take_profit_record_rejects_a_negative_level_index():
    with pytest.raises(ValueError, match="level_index"):
        _record_tp(exit_rule_kind="scaled_take_profit", level_index=-1)


def test_take_profit_record_rejects_a_negative_entry_bar():
    with pytest.raises(ValueError, match="entry_bar"):
        _record_tp(entry_bar=-1)


@pytest.mark.parametrize("exit_bar", [0, 1])
def test_take_profit_record_exit_bar_must_be_strictly_after_entry_bar(exit_bar):
    with pytest.raises(ValueError, match="exit_bar"):
        _record_tp(entry_bar=1, exit_bar=exit_bar)


def test_take_profit_record_rejects_a_negative_exit_rule_index():
    with pytest.raises(ValueError, match="exit_rule_index"):
        _record_tp(exit_rule_index=-1)


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_take_profit_record_rejects_nonpositive_or_nonfinite_exit_price(price):
    with pytest.raises(ValueError, match="exit_price"):
        _record_tp(exit_price=price)


def test_take_profit_record_rejects_an_unknown_exit_rule_kind():
    with pytest.raises(ValueError, match="exit_rule_kind"):
        _record_tp(exit_rule_kind="stop_loss", level_index=None)


def test_take_profit_record_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _record_tp().exit_price = 1.0


# ---------------------------------------------------------------------------
# replay_take_profit_family_exits — the (spec, bars) entry point
# ---------------------------------------------------------------------------


def test_replay_opens_from_entry_rules_and_closes_on_the_standalone_take_profit():
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    bars = {
        "AAA": [
            _flat(101.0),  # entry predicate fires (close > 100)
            _bar(100.0, 100.0, 100.0, 100.0),  # entry fills here at open 100
            _bar(101.0, 106.0, 100.0, 105.0),  # clears the 105 target
        ]
    }
    (got,) = replay_take_profit_family_exits(spec, bars)
    assert (got.symbol, got.entry_bar, got.exit_bar, got.exit_price, got.exit_rule_kind) == (
        "AAA",
        1,
        2,
        105.0,
        "take_profit",
    )


def test_replay_opens_from_entry_rules_and_closes_on_a_ladders_final_rung():
    spec = _spec(
        exit_rules=[ScaledTakeProfitRule(levels=[TakeProfitLevel(pct=0.05, qty_fraction=1.0)])]
    )
    bars = {
        "AAA": [
            _flat(101.0),
            _bar(100.0, 100.0, 100.0, 100.0),
            _bar(101.0, 106.0, 100.0, 105.0),
        ]
    }
    (got,) = replay_take_profit_family_exits(spec, bars)
    assert (got.exit_rule_kind, got.level_index, got.exit_price) == ("scaled_take_profit", 0, 105.0)


def test_take_profit_replay_returns_nothing_when_no_entry_fires():
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    bars = {"AAA": [_flat(50.0), _flat(50.0), _bar(50.0, 55.0, 45.0, 52.0)]}
    assert replay_take_profit_family_exits(spec, bars) == []


def test_replay_returns_nothing_when_the_position_never_reaches_any_target():
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _flat(101.0), _flat(102.0)]}
    assert replay_take_profit_family_exits(spec, bars) == []


def test_take_profit_replay_handles_symbols_independently():
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    reaches = [_flat(101.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.0)]
    never = [_flat(101.0), _flat(100.0), _flat(101.0)]
    got = replay_take_profit_family_exits(spec, {"AAA": reaches, "BBB": never})
    assert [r.symbol for r in got] == ["AAA"]


def test_take_profit_replay_passes_slippage_through_to_the_anchor():
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _bar(101.0, 108.0, 100.0, 107.0)]}
    assert (
        replay_take_profit_family_exits(spec, bars, entry_slippage_bps=0.0)[0].exit_price == 105.0
    )
    assert replay_take_profit_family_exits(spec, bars, entry_slippage_bps=200.0)[
        0
    ].exit_price == pytest.approx(107.1)


def test_take_profit_replay_does_not_mutate_its_inputs():
    """Deep-copies the snapshot rather than shallow-copying it: a shallow
    ``list(...)``/``{k: list(v) ...}`` snapshot still shares the same bar/rule
    OBJECTS with the live input, so an in-place field mutation (e.g.
    ``bar.high = ...``) would move both the input and its "snapshot" together
    and this test would falsely report no mutation. A deep copy is
    independent of the input, so it actually catches that case."""
    import copy

    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.0)]}
    exit_rules_before = copy.deepcopy(spec.exit_rules)
    bars_before = copy.deepcopy(bars)
    replay_take_profit_family_exits(spec, bars)
    assert spec.exit_rules == exit_rules_before
    assert bars == bars_before


def test_take_profit_replay_is_deterministic():
    spec = _spec(exit_rules=[TakeProfitRule(pct=0.05)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _bar(101.0, 106.0, 100.0, 105.0)]}
    assert replay_take_profit_family_exits(spec, bars) == replay_take_profit_family_exits(
        spec, bars
    )


# ---------------------------------------------------------------------------
# signal_exit_rules
# ---------------------------------------------------------------------------


def _sig(rhs: float = 95.0, op: str = "<") -> SignalExitRule:
    """A signal-exit rule on the bar's own close.

    ``bar.close < 95`` against the suite's 100-priced bars means "the exit
    predicate fires on a bar that closed at least 5% down" — a decision only
    the bar's CLOSE can make, which is exactly why this kind fills at the next
    bar's open rather than resting on the book.
    """
    return SignalExitRule(when=Predicate(lhs="bar.close", op=op, rhs=rhs))


def test_signal_exit_rules_returns_only_signal_exit_rule_instances_in_spec_order():
    rules = [
        StopLossRule(pct=0.05),
        _sig(95.0),
        TakeProfitRule(pct=0.1),
        _sig(90.0),
    ]
    got = signal_exit_rules(rules)
    assert [i for i, _ in got] == [1, 3]
    assert all(isinstance(r, SignalExitRule) for _, r in got)


def test_signal_exit_rules_is_not_side_filtered():
    """Unlike a stop's ``basis``, a predicate carries no side concept — the same
    rule list is a candidate set for a long and a short alike."""
    rules = [_sig(95.0)]
    assert signal_exit_rules(rules) == [(0, rules[0])]


# ---------------------------------------------------------------------------
# _PrefixHistoryView
# ---------------------------------------------------------------------------


def _view(bars) -> PandasHistoryView:
    return PandasHistoryView(bars_to_frame(bars), {})


def test_prefix_view_reports_length_one_past_its_index():
    """``length() - 1`` is the index the shared evaluator resolves a signal
    predicate at, so this identity is the whole point of the adapter."""
    view = _view([_flat(100.0), _flat(101.0), _flat(102.0)])
    assert [_PrefixHistoryView(view, i).length() for i in range(3)] == [1, 2, 3]


def test_prefix_view_delegates_reads_to_the_wrapped_view():
    bars = [_flat(100.0), _flat(101.0), _flat(102.0)]
    view = _view(bars)
    prefixed = _PrefixHistoryView(view, 1)
    assert prefixed.bar_field("close", 1) == 101.0
    ref = IndicatorRef(name="sma", params={"period": 2})
    assert prefixed.indicator(ref, 1) == view.indicator(ref, 1)


@pytest.mark.parametrize("i", [-1, 3, 99])
def test_prefix_view_rejects_an_out_of_range_index(i):
    view = _view([_flat(100.0), _flat(101.0), _flat(102.0)])
    with pytest.raises(ValueError, match="out of range"):
        _PrefixHistoryView(view, i)


# ---------------------------------------------------------------------------
# resolve_signal_exit — next-bar-open fill mechanics
# ---------------------------------------------------------------------------


def _resolve_sig(rules, bars, side="long", entry_bar=1, price=100.0):
    return resolve_signal_exit(rules, _entry(side=side, entry_bar=entry_bar, price=price), bars)


def test_signal_exit_fills_at_the_next_bars_open():
    """The defining difference from every resting kind: trigger bar and fill
    bar are not the same bar."""
    bars = [
        _flat(100.0),
        _flat(100.0),  # entry bar; close 100 does not satisfy close < 95
        _bar(100.0, 101.0, 90.0, 94.0),  # trigger bar: closes below 95
        _bar(93.0, 95.0, 92.0, 93.0),  # fill bar: settles at this open
        _flat(93.0),
    ]
    got = _resolve_sig([_sig(95.0)], bars)
    assert (got.exit_bar, got.exit_price, got.exit_rule_kind) == (3, 93.0, "signal_exit")
    assert got.exit_rule_index == 0


def test_signal_exit_is_eligible_on_the_entry_bar_itself():
    """The other half of the non-resting treatment: a resting order is skipped
    on its own materialization bar (see
    ``test_stop_is_not_eligible_on_its_own_entry_bar``), a signal predicate is
    not — only its FILL is deferred."""
    bars = [
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),  # entry bar AND trigger bar
        _bar(93.0, 95.0, 92.0, 93.0),
        _flat(93.0),
    ]
    got = _resolve_sig([_sig(95.0)], bars, entry_bar=1)
    assert got.exit_bar == 2  # entry_bar + 1, the earliest fill this kind allows
    assert got.exit_bar > got.entry_bar  # the record invariant still holds


def test_signal_exit_date_comes_from_the_fill_bar_not_the_trigger_bar():
    bars = [
        _flat(100.0, "2024-03-01T00:00:00"),
        _flat(100.0, "2024-03-02T00:00:00"),
        _bar(100.0, 101.0, 90.0, 94.0, "2024-03-03T00:00:00"),
        _bar(93.0, 95.0, 92.0, 93.0, "2024-03-04T00:00:00"),
    ]
    assert _resolve_sig([_sig(95.0)], bars).exit_date == "2024-03-04"


def test_a_signal_firing_on_the_final_bar_emits_no_record():
    """The documented final-bar rule, identical to the entry side's: no next
    bar to fill against means no trade, never a fabricated fill past the end
    of the data."""
    bars = [_flat(100.0), _flat(100.0), _bar(100.0, 101.0, 90.0, 94.0)]
    assert _resolve_sig([_sig(95.0)], bars) is None


def test_a_signal_firing_on_the_second_to_last_bar_fills_on_the_last_bar():
    """The boundary one bar earlier than the final-bar rule still produces a
    record — guards an off-by-one in the other direction."""
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
        _bar(93.0, 95.0, 92.0, 93.0),
    ]
    assert _resolve_sig([_sig(95.0)], bars).exit_bar == 3


def test_no_signal_exit_rule_produces_no_exit():
    """Also pins cross-kind isolation: the stop below breaches on the trigger
    bar and is still not selected, because this resolver owns one kind."""
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
        _bar(93.0, 95.0, 92.0, 93.0),
    ]
    assert _resolve_sig([StopLossRule(pct=0.05)], bars) is None


def test_a_stop_that_would_also_fire_does_not_displace_the_signal_exit():
    """The positive half of the same isolation: a spec carrying both kinds
    still reports the signal close here. Which of the two production would
    actually have filled is the combined simulator's FIFO question, not this
    module's."""
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),  # breaches a 5% stop AND closes < 95
        _bar(93.0, 95.0, 92.0, 93.0),
    ]
    got = _resolve_sig([StopLossRule(pct=0.05), _sig(95.0)], bars)
    assert (got.exit_rule_kind, got.exit_rule_index, got.exit_bar) == ("signal_exit", 1, 3)


def test_signal_exit_never_fires_when_no_predicate_is_satisfied():
    bars = [_flat(100.0), _flat(100.0), _flat(101.0), _flat(102.0)]
    assert _resolve_sig([_sig(95.0)], bars) is None


def test_out_of_range_entry_bar_is_rejected_for_a_signal_exit():
    with pytest.raises(ValueError, match="out of range"):
        _resolve_sig([_sig(95.0)], [_flat(100.0), _flat(100.0)], entry_bar=5)


# ---------------------------------------------------------------------------
# resolve_signal_exit — spec-order priority
# ---------------------------------------------------------------------------


def test_lowest_spec_index_signal_rule_wins_when_two_fire_on_one_bar():
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),  # satisfies both thresholds below
        _bar(93.0, 95.0, 92.0, 93.0),
    ]
    assert _resolve_sig([_sig(99.0), _sig(95.0)], bars).exit_rule_index == 0


def test_a_later_signal_rule_still_fires_when_the_earlier_one_does_not():
    """Proves the winner is chosen by spec index, not by rule order luck."""
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
        _bar(93.0, 95.0, 92.0, 93.0),
    ]
    assert _resolve_sig([_sig(50.0), _sig(95.0)], bars).exit_rule_index == 1


# ---------------------------------------------------------------------------
# resolve_signal_exit — look-ahead containment
# ---------------------------------------------------------------------------


def test_a_signal_satisfied_only_on_the_last_bar_never_fires_earlier():
    """The look-ahead regression this module's prefix view exists to prevent.

    The shared evaluator resolves a signal predicate at ``view.length() - 1``.
    Handing it this module's whole-history view unwrapped would evaluate the
    LAST bar's close on every step, so this spec would appear to exit at
    ``entry_bar + 1``. Only the last bar satisfies ``close < 95`` here, and it
    has no bar to fill against, so the correct answer is no record at all.
    """
    bars = [
        _flat(100.0),
        _flat(100.0),
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
    ]
    assert _resolve_sig([_sig(95.0)], bars) is None


def test_a_signal_fires_on_its_own_bar_not_on_a_later_bars_history():
    """Same containment, stated positively so a wrong answer is a wrong BAR
    rather than only a missing record: the trigger is bar 3, so the fill is
    bar 4 — not bar 2, which is what evaluating the last bar every step would
    produce."""
    bars = [
        _flat(100.0),
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
        _bar(93.0, 95.0, 92.0, 93.0),
    ]
    assert _resolve_sig([_sig(95.0)], bars).exit_bar == 4


def test_an_indicator_predicate_is_resolved_at_the_trigger_bar():
    """Indicator reads carry the bar index too, not just bar fields.

    ``close < sma(2)`` holds only on bar 3 (90 < 95); on bar 4 the average has
    caught up (100 < 95 is false) and on the flat bars close equals the
    average. Evaluating the final bar every step would find no trigger at all
    and return ``None``.
    """
    bars = [
        _flat(100.0),
        _flat(100.0),
        _flat(100.0),
        _bar(90.0, 100.0, 90.0, 90.0),
        _bar(93.0, 100.0, 93.0, 100.0),
        _flat(100.0),
    ]
    rule = SignalExitRule(
        when=Predicate(lhs="bar.close", op="<", rhs=IndicatorRef(name="sma", params={"period": 2}))
    )
    assert _resolve_sig([rule], bars).exit_bar == 4


@pytest.mark.parametrize("price", [1.0, 100.0, 10_000.0])
def test_the_signal_exit_is_independent_of_the_entry_price(price):
    """Pins the reasoning behind the absent ``entry_slippage_bps`` parameter: a
    signal predicate reads only the history view, and the non-signal intents
    the shared evaluator also computes off ``PositionState.entry_price`` are
    discarded unread. Nothing about the entry can move this record."""
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
        _bar(93.0, 95.0, 92.0, 93.0),
    ]
    got = _resolve_sig([_sig(95.0)], bars, price=price)
    assert (got.exit_bar, got.exit_price) == (3, 93.0)


# ---------------------------------------------------------------------------
# resolve_signal_exit — fill-price rounding and degenerate fill bars
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fill_open", "expected"),
    [(9.123456, 9.1235), (100.123456, 100.12)],
    ids=["four_decimals_below_ten", "two_decimals_at_or_above_ten"],
)
def test_signal_exit_price_is_rounded_to_the_production_bucket(fill_open, expected):
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
        _bar(fill_open, fill_open + 1.0, fill_open - 1.0, fill_open),
    ]
    assert _resolve_sig([_sig(95.0)], bars).exit_price == expected


@pytest.mark.parametrize(
    "bad_open",
    [0.0, -5.0, float("nan"), float("inf"), 0.00004],
    ids=["zero", "negative", "nan", "inf", "rounds_away_to_zero"],
)
def test_an_unusable_fill_bar_open_is_skipped_and_a_later_trigger_still_fires(bad_open):
    """The design doc's uniform nonpositive-exit-reference rule: a degenerate
    bar suppresses one candidate fill, it does not abort the walk. ``0.00004``
    passes the positive-and-finite guard and is caught only by the
    rounds-away-to-zero check, which is why it is exercised here too."""
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),  # trigger; its fill bar is unusable
        _bar(bad_open, 95.0, 90.0, 93.0),  # unusable open, but closes < 95 too
        _bar(93.0, 95.0, 92.0, 93.0),  # so this bar becomes the fill
    ]
    got = _resolve_sig([_sig(95.0)], bars)
    assert (got.exit_bar, got.exit_price) == (4, 93.0)


def test_an_unusable_fill_bar_open_with_no_later_trigger_produces_no_record():
    bars = [
        _flat(100.0),
        _flat(100.0),
        _bar(100.0, 101.0, 90.0, 94.0),
        _bar(0.0, 101.0, 99.0, 100.0),  # unusable open and closes back above 95
        _flat(100.0),
    ]
    assert _resolve_sig([_sig(95.0)], bars) is None


# ---------------------------------------------------------------------------
# ReferenceSignalExit value-object contract
# ---------------------------------------------------------------------------


def _record_sig(**overrides) -> ReferenceSignalExit:
    """A valid signal-exit record; ``overrides`` bend one field at a time."""
    kwargs = {
        "symbol": "AAA",
        "entry_bar": 1,
        "exit_bar": 4,
        "exit_date": "2024-01-05",
        "exit_price": 93.0,
        "exit_rule_kind": "signal_exit",
        "exit_rule_index": 0,
    }
    kwargs.update(overrides)
    return ReferenceSignalExit(**kwargs)


def test_valid_signal_record_constructs():
    assert _record_sig().exit_price == 93.0


def test_signal_record_rejects_a_negative_entry_bar():
    with pytest.raises(ValueError, match="entry_bar"):
        _record_sig(entry_bar=-1)


@pytest.mark.parametrize("exit_bar", [0, 1])
def test_signal_record_exit_bar_must_be_strictly_after_entry_bar(exit_bar):
    """Strict even though a signal predicate may FIRE on ``entry_bar``: the
    fill is always the following bar, so a same-bar close is unrepresentable."""
    with pytest.raises(ValueError, match="exit_bar"):
        _record_sig(entry_bar=1, exit_bar=exit_bar)


def test_signal_record_rejects_a_negative_exit_rule_index():
    with pytest.raises(ValueError, match="exit_rule_index"):
        _record_sig(exit_rule_index=-1)


@pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
def test_signal_record_rejects_nonpositive_or_nonfinite_exit_price(price):
    with pytest.raises(ValueError, match="exit_price"):
        _record_sig(exit_price=price)


def test_signal_record_rejects_a_wrong_exit_rule_kind():
    with pytest.raises(ValueError, match="exit_rule_kind"):
        _record_sig(exit_rule_kind="stop_loss")


def test_signal_record_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _record_sig().exit_price = 1.0


def test_signal_record_carries_no_level_index():
    """Absent by design rather than present-and-``None``, the same stance
    ``ReferenceStopLossExit`` takes: a signal close never carries a rung."""
    assert not hasattr(_record_sig(), "level_index")


def test_signal_exit_kind_distinguishes_it_from_the_resting_kinds():
    """The four modeled kinds are distinguishable by ``exit_rule_kind`` alone,
    which is what a later matching step attributes trades by."""
    assert {
        _record_sig().exit_rule_kind,
        _record().exit_rule_kind,
        _record_tp().exit_rule_kind,
        _record_tp(exit_rule_kind="scaled_take_profit", level_index=1).exit_rule_kind,
    } == {
        "signal_exit",
        "stop_loss",
        "take_profit",
        "scaled_take_profit",
    }


# ---------------------------------------------------------------------------
# replay_signal_exits
# ---------------------------------------------------------------------------


def test_signal_replay_opens_from_entry_rules_and_closes_on_the_signal():
    spec = _spec(exit_rules=[_sig(95.0)])
    bars = {
        "AAA": [
            _flat(101.0),  # entry predicate fires (close > 100)
            _flat(100.0),  # entry fills here at open 100
            _bar(100.0, 101.0, 90.0, 94.0),  # signal predicate fires
            _bar(93.0, 95.0, 92.0, 93.0),  # fills at this open
        ]
    }
    (got,) = replay_signal_exits(spec, bars)
    assert (got.symbol, got.entry_bar, got.exit_bar, got.exit_price) == ("AAA", 1, 3, 93.0)


def test_signal_replay_returns_nothing_when_no_entry_fires():
    spec = _spec(exit_rules=[_sig(95.0)])
    bars = {"AAA": [_flat(50.0), _flat(50.0), _bar(50.0, 50.0, 10.0, 20.0), _flat(20.0)]}
    assert replay_signal_exits(spec, bars) == []


def test_signal_replay_returns_nothing_when_the_position_never_signals():
    spec = _spec(exit_rules=[_sig(95.0)])
    bars = {"AAA": [_flat(101.0), _flat(100.0), _flat(101.0), _flat(102.0)]}
    assert replay_signal_exits(spec, bars) == []


def test_signal_replay_handles_symbols_independently():
    spec = _spec(exit_rules=[_sig(95.0)])
    signals = [_flat(101.0), _flat(100.0), _bar(100.0, 101.0, 90.0, 94.0), _flat(93.0)]
    never_signals = [_flat(101.0), _flat(100.0), _flat(101.0), _flat(102.0)]
    got = replay_signal_exits(spec, {"AAA": signals, "BBB": never_signals})
    assert [r.symbol for r in got] == ["AAA"]


def test_signal_replay_respects_target_symbol_gating():
    spec = _spec(exit_rules=[_sig(95.0)], target_symbols=["AAA"])
    series = [_flat(101.0), _flat(100.0), _bar(100.0, 101.0, 90.0, 94.0), _flat(93.0)]
    got = replay_signal_exits(spec, {"AAA": list(series), "ZZZ": list(series)})
    assert [r.symbol for r in got] == ["AAA"]


def test_signal_replay_indexes_against_the_working_rule_list():
    """A short spec gets the safety stop appended, so the authored signal rule
    keeps its own index while the injected stop takes ``len(spec.exit_rules)``
    — the indices must agree across every kind's replay."""
    spec = _spec(exit_rules=[_sig(95.0)], entry_side="short")
    bars = {
        "AAA": [
            _flat(101.0),
            _flat(100.0),
            _bar(100.0, 101.0, 90.0, 94.0),
            _bar(93.0, 95.0, 92.0, 93.0),
        ]
    }
    assert replay_signal_exits(spec, bars)[0].exit_rule_index == 0
    assert len(working_exit_rules(spec)) == 2


def test_signal_replay_rejects_a_custom_code_spec():
    spec = _spec(exit_rules=[_sig(95.0)])
    spec.requires_custom_code = True
    with pytest.raises(ValueError, match="requires_custom_code"):
        replay_signal_exits(spec, {"AAA": [_flat(101.0), _flat(100.0)]})


def test_signal_replay_does_not_mutate_its_inputs():
    spec = _spec(exit_rules=[_sig(95.0)], entry_side="short")
    bars = {
        "AAA": [
            _flat(101.0),
            _flat(100.0),
            _bar(100.0, 101.0, 90.0, 94.0),
            _bar(93.0, 95.0, 92.0, 93.0),
        ]
    }
    exit_rules_before = list(spec.exit_rules)
    bars_before = {k: list(v) for k, v in bars.items()}
    replay_signal_exits(spec, bars)
    assert list(spec.exit_rules) == exit_rules_before
    assert {k: list(v) for k, v in bars.items()} == bars_before


def test_signal_replay_is_deterministic():
    spec = _spec(exit_rules=[_sig(95.0)])
    bars = {
        "AAA": [
            _flat(101.0),
            _flat(100.0),
            _bar(100.0, 101.0, 90.0, 94.0),
            _bar(93.0, 95.0, 92.0, 93.0),
        ]
    }
    assert replay_signal_exits(spec, bars) == replay_signal_exits(spec, bars)
