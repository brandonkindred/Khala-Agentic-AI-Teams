"""Unit tests for ``executor.reference_entries``."""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.executor.reference_entries import (
    ReferenceEntryFill,
    replay_entry_rules,
)
from investment_team.strategy_lab.spec_dsl import EntryRule, Predicate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _Bar:
    """Minimal ``Bar``-shaped stand-in — the module only reads these attrs."""

    open: float
    high: float
    low: float
    close: float
    volume: float = 1000.0
    timestamp: str = "2024-01-01T00:00:00"
    symbol: str = "AAA"


def _bar(o: float, h: float, low: float, c: float, ts: str = "2024-01-01T00:00:00") -> _Bar:
    return _Bar(open=o, high=h, low=low, close=c, timestamp=ts)


def _spec_with_rules(
    entry_rules: list[EntryRule], target_symbols: list[str] | None = None
) -> StrategySpec:
    """Build the suite's standard test ``StrategySpec`` with explicit entry rules.

    ``target_symbols=None`` disables target-symbol gating (``target_symbols or []``).
    """
    return StrategySpec(
        strategy_id="strat-ref-entries-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=entry_rules,
        target_symbols=target_symbols or [],
    )


def _spec(target_symbols: list[str] | None = None) -> StrategySpec:
    """Default single-rule long spec (``bar.close > 100``) used by most tests."""
    return _spec_with_rules(
        [EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=100.0))],
        target_symbols=target_symbols,
    )


def _close_after_bars(
    fill: ReferenceEntryFill, bars: list[_Bar], hold_bars: int
) -> tuple[int, float]:
    """Test-only stand-in for exit-side replay (real exit rules arrive later).

    Not production logic: ``replay_entry_rules`` models entries only, so
    these tests close a reference position by a fixed hold-bar-count (or the
    series' last bar, whichever comes first) purely to exercise a complete
    open+close round trip on hand-constructed series. Returns
    ``(exit_bar, exit_price)`` using the exit bar's close.
    """
    exit_bar = min(fill.entry_bar + hold_bars, len(bars) - 1)
    return exit_bar, bars[exit_bar].close


# ---------------------------------------------------------------------------
# replay_entry_rules
# ---------------------------------------------------------------------------


def test_mid_series_trigger_fills_at_next_bar_open():
    # Distinct timestamps per bar so entry_date can only match if it is
    # derived from the fill bar (index 2), not the trigger bar (index 1).
    bars = {
        "AAA": [
            _bar(90, 90, 90, 90, ts="2024-01-01T00:00:00"),
            _bar(101, 101, 101, 101, ts="2024-01-02T00:00:00"),
            _bar(102, 103, 101, 102, ts="2024-01-03T00:00:00"),
            _bar(103, 104, 102, 103, ts="2024-01-04T00:00:00"),
        ]
    }
    out = replay_entry_rules(_spec(), bars)
    assert len(out) == 1
    fill = out[0]
    assert fill.symbol == "AAA"
    assert fill.side == "long"
    assert fill.entry_bar == 2
    assert fill.entry_price == 102
    assert fill.entry_rule_index == 0
    assert fill.entry_date == "2024-01-03"  # the fill bar's date, not the trigger bar's


def test_trigger_on_final_bar_produces_no_record():
    bars = {"AAA": [_bar(90, 90, 90, 90), _bar(90, 90, 90, 90), _bar(101, 101, 101, 101)]}
    assert replay_entry_rules(_spec(), bars) == []


def test_no_entry_signal_produces_no_record():
    bars = {"AAA": [_bar(90, 90, 90, 90), _bar(91, 91, 91, 91), _bar(92, 92, 92, 92)]}
    assert replay_entry_rules(_spec(), bars) == []


def test_predicate_true_across_consecutive_bars_suppresses_to_one_fill():
    bars = {
        "AAA": [
            _bar(101, 101, 101, 101),
            _bar(102, 102, 102, 102),
            _bar(103, 103, 103, 103),
            _bar(104, 104, 104, 104),
            _bar(105, 105, 105, 105),
        ]
    }
    out = replay_entry_rules(_spec(), bars)
    assert len(out) == 1
    assert out[0].entry_bar == 1


def test_entry_rule_index_and_side_come_from_the_matching_rule():
    # Two entry rules on the spec, firing on different bars: rule 0 (long,
    # close > 200) never fires in this series; rule 1 (short, close < 50)
    # fires at bar 1. Proves entry_rule_index/side are derived from whichever
    # rule actually matched — not hardcoded to index 0 — and that once rule 1
    # fills the symbol, rule 0's later-satisfiable condition (bar 3's
    # close=250) is never reached, since the module never re-opens a symbol
    # once filled. This does NOT exercise a same-bar tie-break between rules —
    # see test_multiple_entry_rules_same_bar_tie_break_picks_lowest_index for
    # that.
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=200.0)),
        EntryRule(side="short", when=Predicate(lhs="bar.close", op="<", rhs=50.0)),
    ]
    bars = {
        "AAA": [
            _bar(70, 70, 70, 70, ts="2024-01-01T00:00:00"),
            _bar(40, 40, 40, 40, ts="2024-01-02T00:00:00"),
            _bar(44, 46, 43, 45, ts="2024-01-03T00:00:00"),
            _bar(250, 250, 250, 250, ts="2024-01-04T00:00:00"),
        ]
    }
    out = replay_entry_rules(_spec_with_rules(rules), bars)
    assert len(out) == 1
    fill = out[0]
    assert fill.side == "short"
    assert fill.entry_bar == 2
    assert fill.entry_price == 44
    assert fill.entry_rule_index == 1
    assert fill.entry_date == "2024-01-03"


def test_multiple_entry_rules_same_bar_tie_break_picks_lowest_index():
    # Rule 0 (long, close > 90) and rule 1 (short, close > 95) are both false
    # at bar 0's close of 30 and both true at bar 1's close of 100 — a
    # genuine same-bar tie, unlike the mixed-bar scenario above.
    # evaluate_entry_rules scans rules in list order and returns the first
    # match, so rule 0 (the lower index) must win.
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=90.0)),
        EntryRule(side="short", when=Predicate(lhs="bar.close", op=">", rhs=95.0)),
    ]
    bars = {
        "AAA": [
            _bar(30, 30, 30, 30, ts="2024-01-01T00:00:00"),
            _bar(100, 100, 100, 100, ts="2024-01-02T00:00:00"),
            _bar(101, 102, 100, 101, ts="2024-01-03T00:00:00"),
        ]
    }
    out = replay_entry_rules(_spec_with_rules(rules), bars)
    assert len(out) == 1
    fill = out[0]
    assert fill.side == "long"
    assert fill.entry_rule_index == 0
    assert fill.entry_bar == 2
    assert fill.entry_price == 101


def test_nonpositive_fill_bar_open_is_dropped_but_later_trigger_still_fires():
    bars = {
        "AAA": [
            _bar(101, 101, 101, 101),
            _bar(0.0, 1, 1, 90),
            _bar(90, 90, 90, 90),
            _bar(101, 101, 101, 101),
            _bar(105, 105, 105, 105),
        ]
    }
    out = replay_entry_rules(_spec(), bars)
    assert len(out) == 1
    assert out[0].entry_bar == 4


def test_nan_fill_bar_open_is_dropped():
    bars = {"AAA": [_bar(101, 101, 101, 101), _bar(math.nan, 1, 1, 90)]}
    assert replay_entry_rules(_spec(), bars) == []


def test_target_symbols_excludes_untargeted_symbol():
    bars = {
        "AAA": [
            _bar(90, 90, 90, 90),
            _bar(101, 101, 101, 101),
            _bar(102, 103, 101, 102),
        ]
    }
    assert replay_entry_rules(_spec(target_symbols=["OTHER"]), bars) == []


def test_empty_bar_sequence_is_skipped_without_error():
    assert replay_entry_rules(_spec(), {"AAA": []}) == []


def test_symbols_replay_independently():
    # BBB's closes never cross the rule's 100.0 threshold, so only AAA fills —
    # proves each symbol is evaluated against its own bar sequence, not a
    # shared/aggregated one.
    bars = {
        "AAA": [_bar(90, 90, 90, 90), _bar(101, 101, 101, 101), _bar(102, 103, 101, 102)],
        "BBB": [_bar(50, 50, 50, 50), _bar(51, 51, 51, 51), _bar(52, 52, 52, 52)],
    }
    out = replay_entry_rules(_spec(), bars)
    assert [(f.symbol, f.entry_bar) for f in out] == [("AAA", 2)]


def test_suppression_is_per_symbol():
    # Both symbols trigger on bar 0 and stay above threshold afterward; if
    # suppression state were accidentally shared across symbols, BBB would
    # produce no fill once AAA's suppression "latched".
    bars = {
        "AAA": [_bar(101, 101, 101, 101), _bar(102, 102, 102, 102), _bar(103, 103, 103, 103)],
        "BBB": [_bar(101, 101, 101, 101), _bar(102, 102, 102, 102), _bar(103, 103, 103, 103)],
    }
    out = replay_entry_rules(_spec(), bars)
    assert sorted((f.symbol, f.entry_bar) for f in out) == [("AAA", 1), ("BBB", 1)]


# ---------------------------------------------------------------------------
# Round-trip: entry fill closed by the test-only ``_close_after_bars`` helper
# (temporary scaffolding — real exit-side replay is not implemented yet)
# ---------------------------------------------------------------------------


def test_position_closed_by_fixed_hold_bar_count_before_series_end():
    # Entry fires at bar 1, fills at bar 2 (open=102). Holding for 1 bar
    # closes at bar 3 (2 + 1), strictly before the series' last bar (index
    # 4) — so only the fixed-count path can produce this result; a helper
    # that ignored hold_bars and always fell back to the last bar would
    # close at bar 4 instead and fail this assertion.
    bars = [
        _bar(90, 90, 90, 90),
        _bar(101, 101, 101, 101),
        _bar(102, 103, 101, 102),
        _bar(103, 104, 102, 103),
        _bar(104, 105, 103, 104.5),
    ]
    out = replay_entry_rules(_spec(), {"AAA": bars})
    assert len(out) == 1
    fill = out[0]
    assert fill.entry_bar == 2

    exit_bar, exit_price = _close_after_bars(fill, bars, hold_bars=1)
    assert exit_bar == 3
    assert exit_price == 103


def test_position_closed_at_end_of_series_when_hold_bar_count_overruns():
    # Entry fills at bar 2 in a 4-bar series (last index 3). A 5-bar hold
    # would overrun the series (2 + 5 = 7 > 3), so the test-only helper
    # falls back to closing at the series' last bar instead.
    bars = [
        _bar(90, 90, 90, 90),
        _bar(101, 101, 101, 101),
        _bar(102, 103, 101, 102),
        _bar(103, 104, 102, 103.25),
    ]
    out = replay_entry_rules(_spec(), {"AAA": bars})
    assert len(out) == 1
    fill = out[0]
    assert fill.entry_bar == 2

    exit_bar, exit_price = _close_after_bars(fill, bars, hold_bars=5)
    assert exit_bar == 3
    assert exit_price == 103.25


# ---------------------------------------------------------------------------
# ReferenceEntryFill.__post_init__
# ---------------------------------------------------------------------------


def _valid_kwargs() -> dict:
    return dict(
        symbol="AAA",
        side="long",
        entry_bar=1,
        entry_date="2024-01-01",
        entry_rule_index=0,
        entry_price=100.0,
    )


def test_reference_entry_fill_accepts_valid_record():
    fill = ReferenceEntryFill(**_valid_kwargs())
    assert fill.entry_price == 100.0


def test_reference_entry_fill_rejects_negative_entry_bar():
    with pytest.raises(ValueError, match="entry_bar"):
        ReferenceEntryFill(**{**_valid_kwargs(), "entry_bar": -1})


def test_reference_entry_fill_rejects_negative_entry_rule_index():
    with pytest.raises(ValueError, match="entry_rule_index"):
        ReferenceEntryFill(**{**_valid_kwargs(), "entry_rule_index": -1})


@pytest.mark.parametrize("bad_price", [0.0, -1.0, math.nan, math.inf])
def test_reference_entry_fill_rejects_invalid_entry_price(bad_price):
    with pytest.raises(ValueError, match="entry_price"):
        ReferenceEntryFill(**{**_valid_kwargs(), "entry_price": bad_price})


def test_reference_entry_fill_rejects_invalid_side():
    with pytest.raises(ValueError, match="side"):
        ReferenceEntryFill(**{**_valid_kwargs(), "side": "sideways"})


def test_reference_entry_fill_is_frozen():
    fill = ReferenceEntryFill(**_valid_kwargs())
    with pytest.raises(dataclasses.FrozenInstanceError):
        fill.entry_price = 200.0
