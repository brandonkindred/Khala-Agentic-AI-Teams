"""Unit tests for ``executor.predicate_evaluator``."""

from __future__ import annotations

import math

import pandas as pd

from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    PandasHistoryView,
    StreamingHistoryView,
    compare,
    evaluate_entry_rules,
    evaluate_predicate,
    evaluate_signal_exit_rules,
    relative_miss,
)
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_df(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1000.0] * len(closes),
        }
    )


def _pandas_view(closes: list[float]) -> PandasHistoryView:
    return PandasHistoryView(_make_df(closes), {})


def _streaming_view(closes: list[float]) -> StreamingHistoryView:
    view = StreamingHistoryView()
    for i, c in enumerate(closes):
        view.append(
            BarRecord(
                timestamp=f"2024-01-{i + 1:02d}",
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=1000.0,
            )
        )
    return view


# ---------------------------------------------------------------------------
# compare()
# ---------------------------------------------------------------------------


def test_compare_less_than():
    assert compare("<", 1.0, 2.0) is True
    assert compare("<", 2.0, 1.0) is False


def test_compare_greater_than():
    assert compare(">", 2.0, 1.0) is True
    assert compare(">", 1.0, 2.0) is False


def test_compare_less_equal():
    assert compare("<=", 1.0, 1.0) is True
    assert compare("<=", 2.0, 1.0) is False


def test_compare_greater_equal():
    assert compare(">=", 1.0, 1.0) is True
    assert compare(">=", 0.5, 1.0) is False


def test_compare_equal():
    assert compare("==", 1.0, 1.0) is True
    assert compare("==", 1.0, 2.0) is False


def test_compare_cross_above():
    assert compare("cross_above", 11.0, 10.0, prev_lhs=9.0, prev_rhs=10.0) is True
    assert compare("cross_above", 11.0, 10.0, prev_lhs=11.0, prev_rhs=10.0) is False
    assert compare("cross_above", 11.0, 10.0) is False


def test_compare_cross_below():
    assert compare("cross_below", 9.0, 10.0, prev_lhs=11.0, prev_rhs=10.0) is True
    assert compare("cross_below", 9.0, 10.0, prev_lhs=9.0, prev_rhs=10.0) is False
    assert compare("cross_below", 9.0, 10.0) is False


# ---------------------------------------------------------------------------
# relative_miss()
# ---------------------------------------------------------------------------


def test_relative_miss_zero():
    assert relative_miss(10.0, 10.0) == 0.0


def test_relative_miss_nonzero():
    assert math.isclose(relative_miss(11.0, 10.0), 1.0 / 11.0)


# ---------------------------------------------------------------------------
# evaluate_predicate() — simple ops
# ---------------------------------------------------------------------------


def test_evaluate_predicate_gt_satisfied():
    pred = Predicate(lhs=IndicatorRef(name="sma", params={"period": 3}), op=">", rhs=50.0)
    view = _pandas_view([40.0, 50.0, 60.0, 70.0, 80.0])
    result = evaluate_predicate(pred, view, 4)
    assert result.status == "satisfied"


def test_evaluate_predicate_gt_miss():
    pred = Predicate(lhs=IndicatorRef(name="sma", params={"period": 3}), op=">", rhs=200.0)
    view = _pandas_view([40.0, 50.0, 60.0, 70.0, 80.0])
    result = evaluate_predicate(pred, view, 4)
    assert result.status == "miss"


def test_evaluate_predicate_warmup():
    pred = Predicate(lhs=IndicatorRef(name="sma", params={"period": 20}), op=">", rhs=50.0)
    view = _pandas_view([40.0, 50.0, 60.0])
    result = evaluate_predicate(pred, view, 2)
    assert result.status == "warmup"


# ---------------------------------------------------------------------------
# evaluate_predicate() — cross ops
# ---------------------------------------------------------------------------


def test_evaluate_predicate_cross_above_satisfied():
    sma_fast = IndicatorRef(name="sma", params={"period": 2})
    sma_slow = IndicatorRef(name="sma", params={"period": 3})
    pred = Predicate(lhs=sma_fast, op="cross_above", rhs=sma_slow)
    closes = [10.0, 12.0, 11.0, 9.0, 8.0, 10.0, 14.0, 18.0]
    view = _pandas_view(closes)
    satisfied_any = False
    for i in range(3, len(closes)):
        r = evaluate_predicate(pred, view, i)
        if r.status == "satisfied":
            satisfied_any = True
            break
    assert satisfied_any


def test_evaluate_predicate_cross_above_not_at_first_bar():
    sma_fast = IndicatorRef(name="sma", params={"period": 2})
    pred = Predicate(lhs=sma_fast, op="cross_above", rhs=5.0)
    view = _pandas_view([10.0, 20.0])
    result = evaluate_predicate(pred, view, 0)
    assert result.status == "warmup"


# ---------------------------------------------------------------------------
# evaluate_entry_rules()
# ---------------------------------------------------------------------------


def test_evaluate_entry_rules_first_match():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=100.0)),
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=50.0)),
    ]
    view = _pandas_view([60.0, 70.0, 80.0])
    result = evaluate_entry_rules(rules, view, 2)
    assert result is not None
    _rule, idx = result
    assert idx == 1


def test_evaluate_entry_rules_no_match():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=100.0)),
    ]
    view = _pandas_view([60.0])
    result = evaluate_entry_rules(rules, view, 0)
    assert result is None


def test_evaluate_entry_rules_side_filter():
    rules = [
        EntryRule(side="long", when=Predicate(lhs="bar.close", op=">", rhs=50.0)),
        EntryRule(side="short", when=Predicate(lhs="bar.close", op=">", rhs=50.0)),
    ]
    view = _pandas_view([60.0])
    result = evaluate_entry_rules(rules, view, 0, side_filter="short")
    assert result is not None
    _rule, idx = result
    assert idx == 1


# ---------------------------------------------------------------------------
# evaluate_signal_exit_rules()
# ---------------------------------------------------------------------------


def test_evaluate_signal_exit_rules_match():
    rules = [
        SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=100.0)),
    ]
    view = _pandas_view([110.0])
    result = evaluate_signal_exit_rules(rules, view, 0)
    assert result is not None


def test_evaluate_signal_exit_rules_no_match():
    rules = [
        SignalExitRule(when=Predicate(lhs="bar.close", op=">", rhs=100.0)),
    ]
    view = _pandas_view([50.0])
    result = evaluate_signal_exit_rules(rules, view, 0)
    assert result is None


# ---------------------------------------------------------------------------
# StreamingHistoryView parity with PandasHistoryView
# ---------------------------------------------------------------------------


def test_streaming_pandas_parity():
    closes = [
        100.0,
        101.0,
        102.0,
        103.0,
        104.0,
        105.0,
        106.0,
        107.0,
        108.0,
        109.0,
        110.0,
        111.0,
        112.0,
        113.0,
        114.0,
        115.0,
        116.0,
        117.0,
        118.0,
        119.0,
    ]
    pandas_view = _pandas_view(closes)
    streaming = _streaming_view(closes)
    ref = IndicatorRef(name="sma", params={"period": 5})
    last = len(closes) - 1
    p_val = pandas_view.indicator(ref, last)
    s_val = streaming.indicator(ref, last)
    assert p_val is not None and s_val is not None
    assert math.isclose(p_val, s_val, rel_tol=1e-9)


def test_streaming_view_bar_field():
    view = _streaming_view([100.0, 200.0])
    assert view.bar_field("close", 0) == 100.0
    assert view.bar_field("close", 1) == 200.0
    assert view.bar_field("high", 1) == 201.0


def test_streaming_view_length():
    view = _streaming_view([1.0, 2.0, 3.0])
    assert view.length() == 3


def test_streaming_view_cache_invalidation():
    view = StreamingHistoryView()
    for c in [100.0, 101.0, 102.0]:
        view.append(
            BarRecord(timestamp="2024-01-01", open=c, high=c + 1, low=c - 1, close=c, volume=1000.0)
        )
    ref = IndicatorRef(name="sma", params={"period": 2})
    v1 = view.indicator(ref, 2)
    view.append(
        BarRecord(timestamp="2024-01-02", open=200, high=201, low=199, close=200, volume=1000.0)
    )
    v2 = view.indicator(ref, 3)
    assert v1 is not None and v2 is not None
    assert v1 != v2
