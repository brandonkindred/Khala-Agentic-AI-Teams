"""Unit tests for ``executor.reference_entries``."""

from __future__ import annotations

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


def _spec(target_symbols: list[str] | None = None) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-ref-entries-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=100.0)),
        ],
        target_symbols=target_symbols or [],
    )


# ---------------------------------------------------------------------------
# replay_entry_rules
# ---------------------------------------------------------------------------


def test_mid_series_trigger_fills_at_next_bar_open():
    bars = {
        "AAA": [
            _bar(90, 90, 90, 90),
            _bar(101, 101, 101, 101),
            _bar(102, 103, 101, 102),
            _bar(103, 104, 102, 103),
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
    assert fill.entry_date == "2024-01-01"


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
