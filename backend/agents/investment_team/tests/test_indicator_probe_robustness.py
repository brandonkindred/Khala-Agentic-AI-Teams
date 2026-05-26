"""Robustness tests for the indicator-coverage probe."""

from __future__ import annotations

import inspect
import textwrap

import numpy as np
import pandas as pd
import pytest

from investment_team.models import CoverageCategory
from investment_team.strategy_lab.coverage_probe import run_indicator_probe

from ._indicator_probe_fixtures import (
    flat_close_df,
    flat_ohlcv,
    make_strategy,
    small_swing_df,
    swing_close_df,
)

CC = CoverageCategory


# ────────────────────────────────────────────────────────────────────────
# § 1  Simple predicate → category on flat OHLCV (close=100)
# ────────────────────────────────────────────────────────────────────────

_FLAT_CASES: list[tuple[str, CoverageCategory, str, str]] = [
    # (predicate, expected_category, preamble, init)
    ("close > 0", CC.COVERAGE_OK, "", ""),
    ("close < -50", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("close > 1000", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("volume > 0", CC.COVERAGE_OK, "", ""),
    ("rsi(close) < 25", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("rsi(self.history) < 25", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("sma(close, PERIOD + 1) > 100", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("rsi(close) < -50", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("close > sma(close, 0)", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close > sma(close, 2.5)", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close > sma(close, period=-5)", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("macd(close, PERIOD + 1)[0] > 0", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("macd(close, fast=dynamic_fast)[0] > 0", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    (
        "close > bollinger_bands(close, 20, num_std=self.band_width)[0]",
        CC.UNKNOWN_LOW_COVERAGE,
        "",
        "",
    ),
    (
        "close > bollinger_bands(close, 20, num_std=0.1)[0]",
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "",
        "",
    ),
    ("macd(close, 5, 10, 4)[0] > 0", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("atr(high, low, close, 14) > 0.5", CC.COVERAGE_OK, "", ""),
    ("atr() > 0.5", CC.COVERAGE_OK, "", ""),
    ("stochastic(high, low, close, 3)[0] > 0", CC.COVERAGE_OK, "", ""),
    ("self.custom_ok(bar) or close < -50", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("self.custom_ok(bar) or close > 0", CC.COVERAGE_OK, "", ""),
    ("self.custom_ok(bar) or close < -999", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("(close > 0 and self.custom_ok(bar)) or close < -999", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close > 0 and self.custom_ok(bar)", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close < -50 and self.custom_ok(bar)", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("close > 0 and True", CC.COVERAGE_OK, "", ""),
    ('close > 0 and 1 and "enabled"', CC.COVERAGE_OK, "", ""),
    ("close > 0 and 1 < 2", CC.COVERAGE_OK, "", ""),
    ("close > 0 and False", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close > 0 and 1 < 0", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close > 0 and (1 + 1 == 3)", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close > 0 and (1 + 1 == 2)", CC.COVERAGE_OK, "", ""),
    ("close > 0 and (5 % 2 == 0)", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    ("close > 0 and (volume < 0 or close < -1)", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("close > 0 and (volume > 0 or close < -1)", CC.COVERAGE_OK, "", ""),
    ("close > 0 and (self.custom_ok(bar) or close < -50)", CC.UNKNOWN_LOW_COVERAGE, "", ""),
    (
        "close < sma(close, SMA_LOOKBACK) - 100",
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "SMA_LOOKBACK = 5",
        "",
    ),
    ("close > 50 or close > 1000", CC.COVERAGE_OK, "", ""),
    ("close < -10 or close > 1000", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "", ""),
    ("close > 50 or close > 1000 or close < -10", CC.COVERAGE_OK, "", ""),
    ("close > 0 or close < 0", CC.COVERAGE_OK, "", ""),
    (
        "close > 1000\n            if close > 0 or close < 0:\n                pass",
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "",
        "",
    ),  # nested ancestor zero
    (
        "LOWER = -1.0\n                if close < LOWER:",
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "",
        "",
    ),  # negative named threshold — inline local
    (
        "close > bollinger_bands(close, 20, num_std=self.band_width)[0]",
        CC.UNKNOWN_LOW_COVERAGE,
        "",
        "",
    ),
    ("macd(close)[0] > ZERO_LINE", CC.INDICATOR_FILTER_TOO_RESTRICTIVE, "ZERO_LINE = 0", ""),
    (
        "LIMIT = 1\n\nclass S:\n    def on_bar(self, ctx, bar):\n        if close > 0 and LIMIT == 1:\n            pass",
        CC.COVERAGE_OK,
        "",
        "",
    ),  # named constant compare — uses full override below
]


@pytest.mark.parametrize(
    ("predicate", "expected", "preamble", "init"),
    [
        pytest.param(pred, exp, pre, ini, id=pred[:60].replace("\n", "|"))
        for pred, exp, pre, ini in _FLAT_CASES
        if "\n" not in pred  # only simple single-line predicates
    ],
)
def test_flat_ohlcv_predicate(
    predicate: str, expected: CoverageCategory, preamble: str, init: str
) -> None:
    code = make_strategy(predicate, preamble=preamble, init=init)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is expected


# ────────────────────────────────────────────────────────────────────────
# § 1b  Nested / multi-line predicates on flat OHLCV (standalone)
# ────────────────────────────────────────────────────────────────────────


def test_or_under_ancestor_zero_hit_ancestor_still_blocks() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 1000:
                    if close > 0 or close < 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert any("close > 1000" in b.evidence for b in report.likely_blockers)


def test_or_under_ancestor_keeps_or_semantics() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    if close > 0 or close < 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK


def test_or_predicate_with_all_legs_zero_blocker_evidence() -> None:
    code = make_strategy("close < -10 or close > 1000")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.likely_blockers) == 1
    b = report.likely_blockers[0]
    assert b.reason == "or_group_never_fires"
    assert "close < -10" in b.evidence
    assert " OR " in b.evidence


def test_or_predicate_with_one_firing_leg_labels() -> None:
    code = make_strategy("close > 50 or close > 1000")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert len(report.subconditions) == 2
    by_label = {sc.label: sc for sc in report.subconditions}
    assert by_label["close > 50"].hit_count == 60
    assert by_label["close > 1000"].hit_count == 0


def test_nested_or_labels() -> None:
    code = make_strategy("close > 0 and (volume < 0 or close < -1)")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    labels = [sc.label for sc in report.subconditions]
    assert "close > 0" in labels
    assert any("volume < 0" in lbl and "close < -1" in lbl for lbl in labels)


def test_named_constant_compare_does_not_taint_group() -> None:
    code = textwrap.dedent("""
        LIMIT = 1

        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and LIMIT == 1:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK


def test_static_false_named_constant_compare_unreachable() -> None:
    code = textwrap.dedent("""
        LIMIT = 1

        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and LIMIT == 0:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_static_false_conjunct_does_not_block_sibling() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and False:
                    pass
                if close > 0:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK


def test_static_false_conjunct_routes_to_orelse() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and False:
                    pass
                else:
                    if close > 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK


def test_unmodelled_and_conjunct_alongside_clean_stays_ok() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and self.custom_ok(bar):
                    pass
                if close > 0:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK


def test_unknown_and_conjunct_propagates_to_nested_body() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and self.custom_ok(bar):
                    if volume > 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_unknown_and_conjunct_propagates_through_or() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and self.custom_ok(bar):
                    if volume > 0 or close > 50:
                        if close > 25:
                            pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


# ────────────────────────────────────────────────────────────────────────
# § 2  Malformed / empty / edge-case inputs
# ────────────────────────────────────────────────────────────────────────


def test_malformed_strategy_returns_unknown() -> None:
    report = run_indicator_probe(strategy_code="def on_bar(:::", market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE
    assert "did not parse" in report.summary


def test_empty_strategy_code_returns_unknown() -> None:
    report = run_indicator_probe(strategy_code="", market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_no_recognized_subconditions_returns_unknown() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if 1 < 2:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE
    assert report.subconditions == []


def test_empty_market_data_does_not_raise() -> None:
    code = make_strategy("close > 0")
    report = run_indicator_probe(strategy_code=code, market_data={})
    assert report.coverage_category in {CC.COVERAGE_OK, CC.UNKNOWN_LOW_COVERAGE}
    assert report.bars_checked == 0


def test_evaluator_failure_per_subcondition_does_not_raise() -> None:
    df = pd.DataFrame(
        {
            "open": np.full(30, 100.0),
            "high": np.full(30, 101.0),
            "low": np.full(30, 99.0),
            "close": np.full(30, 100.0),
        },
        index=pd.date_range("2024-01-01", periods=30, freq="D"),
    )
    code = make_strategy("volume > 0")
    report = run_indicator_probe(strategy_code=code, market_data={"SYM": df})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert report.subconditions[0].hit_count == 0


def test_no_llm_calls_made() -> None:
    import investment_team.strategy_lab.coverage_probe.indicator_probe as mod

    src = inspect.getsource(mod)
    assert "llm_service" not in src
    assert "LLMClient" not in src
    for name in dir(mod):
        assert "llm" not in name.lower(), f"unexpected llm symbol: {name}"
    code = make_strategy("close > 0")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK


# ────────────────────────────────────────────────────────────────────────
# § 3  Position-check / guard-clause routing
# ────────────────────────────────────────────────────────────────────────


def _assert_pos_check(
    code: str, expected: CoverageCategory, present: set[str], absent: set[str]
) -> None:
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is expected
    labels = {sc.label for sc in report.subconditions}
    for p in present:
        assert p in labels
    for a in absent:
        assert a not in labels


def test_position_check_else_branch_is_skipped() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is None:
                    if close > 0:
                        pass
                else:
                    if close < -50:
                        pass
    """)
    _assert_pos_check(code, CC.COVERAGE_OK, {"close > 0"}, {"close < -50"})


def test_position_check_via_ctx_call() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if ctx.position(bar.symbol) is None:
                    if close > 0:
                        pass
                else:
                    if close < -50:
                        pass
    """)
    _assert_pos_check(code, CC.COVERAGE_OK, {"close > 0"}, {"close < -50"})


def test_inverted_position_check_routes_to_orelse() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is not None:
                    if close < -50:
                        pass
                else:
                    if close > 0:
                        pass
    """)
    _assert_pos_check(code, CC.COVERAGE_OK, {"close > 0"}, {"close < -50"})


def test_combined_position_gate_entry() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is None and close > 0:
                    pass
                elif pos is not None and close < -50:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    labels = {sc.label for sc in report.subconditions}
    assert "close > 0" in labels
    assert "close < -50" not in labels
    assert report.coverage_category is CC.COVERAGE_OK


def test_combined_position_gate_zero_hit_entry() -> None:
    code = make_strategy("pos is None and close < -50")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_vacant_guard_clause_skips_exit() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is None:
                    return
                if close < 0:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is not CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_vacant_guard_clause_with_real_entry() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is None:
                    if close > 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK


# ────────────────────────────────────────────────────────────────────────
# § 4  Bool-call / bare-name residuals
# ────────────────────────────────────────────────────────────────────────

_BOOL_CASES = [
    pytest.param(
        "_entry = sma(close, 5)\n"
        "                pos = ctx.position(bar.symbol)\n"
        "                if pos is None and bool(_entry):",
        CC.COVERAGE_OK,
        "bool(_entry)",
        True,
        id="bool-indicator-name",
    ),
    pytest.param(
        "_entry = sma(close, 5)\n"
        "                pos = ctx.position(bar.symbol)\n"
        "                if pos is None and _entry:",
        CC.COVERAGE_OK,
        "_entry",
        True,
        id="bare-name-truthiness",
    ),
    pytest.param(
        "_entry = self._n_root(bars)\n"
        "                pos = ctx.position(bar.symbol)\n"
        "                if pos is None and bool(_entry):",
        CC.UNKNOWN_LOW_COVERAGE,
        None,
        False,
        id="bool-unbound-name-unknown",
    ),
]


@pytest.mark.parametrize(("body", "expected", "label", "has_hits"), _BOOL_CASES)
def test_bool_and_name_residuals(
    body: str, expected: CoverageCategory, label: str | None, has_hits: bool
) -> None:
    code = textwrap.dedent(f"""
        class S:
            def on_bar(self, ctx, bar):
                {body}
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is expected
    if label is not None:
        assert len(report.subconditions) == 1
        assert report.subconditions[0].label == label
        if has_hits:
            assert report.subconditions[0].hit_count > 0


def test_bool_call_on_compare_delegates() -> None:
    code = make_strategy("bool(close > 50)")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].label == "close > 50"
    assert report.subconditions[0].hit_count > 0


def test_cached_compare_entry_predicate_binds_through_bool() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                _entry = close > sma(close, 5)
                pos = ctx.position(bar.symbol)
                if pos is None and bool(_entry):
                    pass
    """)
    df = flat_ohlcv(n=50)
    df.loc[df.index[25:], "close"] = 105.0
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


# ────────────────────────────────────────────────────────────────────────
# § 5  Nested definition skipping (FunctionDef / AsyncFunctionDef / ClassDef)
# ────────────────────────────────────────────────────────────────────────

_NESTED_DEF_CASES = [
    pytest.param(
        "def debug_helper():\n"
        "                    if close < -50:\n"
        "                        return False\n"
        "                    return True\n"
        "                if close > 0:",
        id="function-def",
    ),
    pytest.param(
        "async def fetch_helper():\n"
        "                    if close < -50:\n"
        "                        return False\n"
        "                    return True\n"
        "                if close > 0:",
        id="async-function-def",
    ),
    pytest.param(
        "class Inner:\n"
        "                    def helper(self):\n"
        "                        if close < -50:\n"
        "                            return False\n"
        "                        return True\n"
        "                if close > 0:",
        id="class-def",
    ),
]


@pytest.mark.parametrize("body", _NESTED_DEF_CASES)
def test_nested_definition_is_skipped(body: str) -> None:
    code = textwrap.dedent(f"""
        class S:
            def on_bar(self, ctx, bar):
                {body}
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert len(report.subconditions) == 1


# ────────────────────────────────────────────────────────────────────────
# § 6  Strategy-class / module constant resolution
# ────────────────────────────────────────────────────────────────────────


def test_prefers_on_bar_over_top_level_helper() -> None:
    code = textwrap.dedent("""
        def generate_signal():
            return None

        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].label == "close > 0"


def test_init_self_assignment_window_is_resolved() -> None:
    code = textwrap.dedent("""
        class S:
            def __init__(self):
                self.WINDOW = 5
            def on_bar(self, ctx, bar):
                if close > sma(close, self.WINDOW):
                    pass
    """)
    df = flat_ohlcv(n=100)
    df.loc[df.index[60:], "close"] = 95.0
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label == "close > sma(close, self.WINDOW)"


def test_negative_named_threshold_is_preserved() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                LOWER = -1.0
                if close < LOWER:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert report.subconditions[0].hit_count == 0


def test_reassignment_to_scalar_clears_stale_indicator() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                threshold = sma(close, 5)
                threshold = 150
                if close > threshold:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv(n=60)})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert report.subconditions[0].hit_count == 0


def test_reassigned_local_uses_latest_binding() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                threshold = sma(close, 5) - 1
                threshold = sma(close, 5) + 1000
                if close > threshold:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert report.subconditions[0].hit_count == 0


def test_reassign_local_to_non_literal_clears_stale() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                LIMIT = 200
                LIMIT = self.dynamic_limit()
                if close > LIMIT:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


# ────────────────────────────────────────────────────────────────────────
# § 7  Tests requiring swing / custom DataFrames
# ────────────────────────────────────────────────────────────────────────


def test_class_attribute_window_resolves(self=None) -> None:
    n = 200
    moves = np.array([+0.005, -0.005] * (n // 2))
    close = 100.0 * np.cumprod(1.0 + moves)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    )
    code_short = textwrap.dedent("""
        class S:
            WINDOW = 3
            def on_bar(self, ctx, bar):
                if close < sma(close, self.WINDOW):
                    pass
    """)
    code_long = textwrap.dedent("""
        class S:
            WINDOW = 50
            def on_bar(self, ctx, bar):
                if close < sma(close, self.WINDOW):
                    pass
    """)
    short = run_indicator_probe(strategy_code=code_short, market_data={"AAPL": df})
    long = run_indicator_probe(strategy_code=code_long, market_data={"AAPL": df})
    assert short.subconditions[0].hit_count != long.subconditions[0].hit_count


def test_derived_threshold_assign_is_bound() -> None:
    df = flat_ohlcv(n=50)
    df.loc[df.index[25:], "close"] = 105.0
    code_named = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                threshold = sma(close, 5) * 1.02
                if close > threshold:
                    pass
    """)
    code_inline = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > sma(close, 5) * 1.02:
                    pass
    """)
    named = run_indicator_probe(strategy_code=code_named, market_data={"AAPL": df})
    inline = run_indicator_probe(strategy_code=code_inline, market_data={"AAPL": df})
    assert named.coverage_category is inline.coverage_category
    assert named.subconditions[0].hit_count == inline.subconditions[0].hit_count
    assert named.subconditions[0].hit_count > 0


def test_atr_positional_period_is_resolved() -> None:
    df = swing_close_df()
    code_short = make_strategy("atr(high, low, close, 2) > 3")
    code_default = make_strategy("atr(high, low, close) > 3")
    short = run_indicator_probe(strategy_code=code_short, market_data={"SYM": df})
    default = run_indicator_probe(strategy_code=code_default, market_data={"SYM": df})
    assert short.subconditions[0].hit_count != default.subconditions[0].hit_count


def test_indicator_kwarg_volume_input_distinguishes_from_close() -> None:
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": np.full(n, 100.0),
            "high": np.full(n, 101.0),
            "low": np.full(n, 99.0),
            "close": np.full(n, 100.0),
            "volume": np.full(n, 2000.0),
        },
        index=idx,
    )
    code = make_strategy("sma(series=volume, period=3) > 1500")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_indicator_kwarg_series_input_is_resolved() -> None:
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    volume = np.array([1000.0, 2000.0] * (n // 2))
    df = pd.DataFrame(
        {
            "open": np.full(n, 100.0),
            "high": np.full(n, 101.0),
            "low": np.full(n, 99.0),
            "close": np.full(n, 100.0),
            "volume": volume,
        },
        index=idx,
    )
    code = make_strategy("sma(series=volume, period=2) > 1500")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert len(report.subconditions) == 1


def test_indicator_with_history_listcomp_input_uses_correct_column() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                vol_avg = sma([b.volume for b in history], 5)
                if volume > vol_avg * 1.5:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv(n=30)})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_named_series_arg_resolves_via_binding() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                closes = [b.close for b in history]
                if rsi(closes, 14) < -50:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_float_threshold_local_is_preserved() -> None:
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = np.array([90.0] * 15 + [110.0] * 15)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                limit = 100.5
                if close > limit:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count == 15


def test_zero_valued_named_threshold_is_preserved() -> None:
    n = 60
    moves = ([+0.005] * 10 + [-0.005] * 10) * 3
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )
    code = textwrap.dedent("""
        ZERO_LINE = 0

        class S:
            def on_bar(self, ctx, bar):
                if macd(close)[0] > ZERO_LINE:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is not CC.UNKNOWN_LOW_COVERAGE
    assert len(report.subconditions) == 1


def test_tuple_unpacked_indicator_outputs_bind() -> None:
    n = 100
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    closes_shock = np.full(n, 100.0)
    closes_shock[::10] = 130.0
    df = pd.DataFrame(
        {
            "open": closes_shock,
            "high": closes_shock * 1.005,
            "low": closes_shock * 0.995,
            "close": closes_shock,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                closes = [b.close for b in history]
                upper, mid, lower = bollinger_bands(closes, 20)
                if close > upper:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.subconditions[0].label == "close > upper"
    assert report.subconditions[0].hit_count > 0


def test_tuple_unpacked_stochastic_binds_hlc() -> None:
    n = 60
    moves = np.array([-0.005] * 30 + [+0.005] * 30)
    closes = 100.0 * np.cumprod(1.0 + moves)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                k, d = stochastic(high, low, close)
                if k < 20:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.subconditions[0].label == "k < 20"
    assert report.coverage_category in {CC.COVERAGE_OK, CC.INDICATOR_FILTER_TOO_RESTRICTIVE}


# ────────────────────────────────────────────────────────────────────────
# § 8  Scope-shadowing (function-local, helper class, exit-branch)
# ────────────────────────────────────────────────────────────────────────


def test_function_local_period_shadows_outer_scope() -> None:
    code = textwrap.dedent("""
        WINDOW = 200

        class S:
            def on_bar(self, ctx, bar):
                WINDOW = 5
                if close > sma(close, WINDOW):
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": small_swing_df()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_exit_branch_reassignment_does_not_shadow_entry() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                ma = sma(close, 5)
                pos = ctx.position(bar.symbol)
                if pos is None:
                    if close > ma:
                        pass
                else:
                    ma = sma(close, 200)
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": small_swing_df()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_later_reassignment_does_not_shadow_earlier() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                ma = sma(close, 5)
                if close > ma:
                    pass
                ma = 999
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": small_swing_df()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_helper_class_does_not_shadow_strategy_constant() -> None:
    code = textwrap.dedent("""
        class Helper:
            PERIOD = 2

        class Strategy:
            PERIOD = 200

            def on_bar(self, ctx, bar):
                if close > sma(close, self.PERIOD):
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": small_swing_df()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert report.subconditions[0].hit_count == 0


def test_helper_class_period_does_not_apply_when_missing() -> None:
    code = textwrap.dedent("""
        WINDOW = 5

        class Helper:
            PERIOD = 999

        class Strategy:
            def on_bar(self, ctx, bar):
                if close > sma(close, WINDOW):
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": small_swing_df()})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_module_helper_function_local_does_not_shadow() -> None:
    n = 60
    moves = ([+0.005] * 10 + [-0.005] * 10) * 3
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )
    code = textwrap.dedent("""
        def helper():
            WINDOW = 999

        class Strategy:
            WINDOW = 2

            def on_bar(self, ctx, bar):
                if close > sma(close, self.WINDOW):
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_strategy_class_constant_overrides_module() -> None:
    n = 30
    close = np.array([100.0] * 15 + [110.0] * 15)
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": close,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )
    code = textwrap.dedent("""
        WINDOW = 1

        class Strategy:
            WINDOW = 3

            def on_bar(self, ctx, bar):
                if close > sma(close, self.WINDOW):
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_helper_method_self_assignment_does_not_shadow_init() -> None:
    code = textwrap.dedent("""
        class Strategy:
            def helper(self):
                self.THRESHOLD = 100

            def __init__(self):
                self.THRESHOLD = 10

            def on_bar(self, ctx, bar):
                if close > self.THRESHOLD:
                    pass
    """)
    df = flat_close_df(50.0)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK
    assert report.subconditions[0].hit_count == 30


def test_local_name_shadowing_ohlcv_uses_local_binding() -> None:
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": np.full(n, 50.0),
            "high": np.full(n, 201.0),
            "low": np.full(n, 49.0),
            "close": np.full(n, 200.0),
            "volume": np.full(n, 1e6),
        },
        index=idx,
    )
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                close = sma(open, 2)
                if close > 100:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_self_close_attribute_is_threshold_not_column() -> None:
    df = flat_close_df(200.0)
    code = textwrap.dedent("""
        class S:
            def __init__(self):
                self.close = 100

            def on_bar(self, ctx, bar):
                if close > self.close:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK


def test_unsupported_tuple_reassignment_clears_stale() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                upper = sma(close, 2)
                upper, lower = self.custom_levels(bar)
                if close > upper:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_constructor_default_false_branch_is_skipped() -> None:
    code = textwrap.dedent("""
        class S:
            WINDOW = 2

            def __init__(self, enabled=False):
                if enabled:
                    self.WINDOW = 200

            def on_bar(self, ctx, bar):
                if close < sma(close, self.WINDOW) - 50:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv(n=30)})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


# ────────────────────────────────────────────────────────────────────────
# § 9  Unresolved period / tuple-indicator kwarg tests
# ────────────────────────────────────────────────────────────────────────

_UNRESOLVED_CASES = [
    pytest.param(
        "close > bollinger_bands(close, PERIOD + 1)\nif close > upper:",
        CC.UNKNOWN_LOW_COVERAGE,
        id="bb-unpack-unresolved",
    ),
]


def test_unresolved_tuple_unpack_skips_binding() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                upper, mid, lower = bollinger_bands(close, PERIOD + 1)
                if close > upper:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_unresolved_tuple_unpack_kwarg_skips_binding() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                upper, mid, lower = bollinger_bands(close, 20, num_std=self.band_width)
                if close > upper:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_stochastic_unpack_explicit_unrecognised_input_skips() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                synth = self.compute_synth_high()
                k, d = stochastic(synth, low, close, 3)
                if k > 50:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_stochastic_unpack_with_recognised_columns_still_works() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                k, d = stochastic(high, low, close, 3)
                if k > 50:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is not CC.UNKNOWN_LOW_COVERAGE
    assert len(report.subconditions) == 1


def test_atr_explicit_unrecognised_input_drops() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                synth = self.compute_synth_high()
                if atr(synth, low, close, 14) > 0.5:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_stochastic_explicit_unrecognised_input_drops() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                synth = self.compute_synth_high()
                if stochastic(synth, low, close, 3)[0] > 0:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_bollinger_num_std_float_is_accepted() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > bollinger_bands(close, 20, num_std=0.1)[0]:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv(n=30)})
    assert report.coverage_category is not CC.UNKNOWN_LOW_COVERAGE


# ────────────────────────────────────────────────────────────────────────
# § 10  Warmup / insufficient-bars tests
# ────────────────────────────────────────────────────────────────────────


def test_warmup_uses_per_symbol_max_history() -> None:
    code = make_strategy("close > sma(close, 150)")
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": flat_ohlcv(n=100), "MSFT": flat_ohlcv(n=100)},
        warmup_bars_required=150,
    )
    assert report.coverage_category is CC.INSUFFICIENT_BARS
    assert report.bars_checked == 200
    assert report.likely_blockers[0].reason == "insufficient_bars"


def test_warmup_passes_when_one_symbol_has_enough() -> None:
    code = make_strategy("close > 0")
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": flat_ohlcv(n=200), "MSFT": flat_ohlcv(n=50)},
        warmup_bars_required=150,
    )
    assert report.coverage_category is CC.COVERAGE_OK


def test_warmup_check_restricted_to_gated_symbols() -> None:
    code = make_strategy('bar.symbol == "AAPL" and close > sma(close, 50)')
    aapl = flat_close_df(100.0, n=10)
    msft = flat_close_df(200.0, n=100)
    report = run_indicator_probe(
        strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft}, warmup_bars_required=50
    )
    assert report.coverage_category is CC.INSUFFICIENT_BARS
    assert "AAPL" in (report.likely_blockers[0].evidence or "")


def test_warmup_check_unaffected_when_any_group_universal() -> None:
    code = make_strategy("close > sma(close, 50)")
    short = flat_close_df(100.0, n=10)
    long = flat_close_df(100.0, n=100)
    report = run_indicator_probe(
        strategy_code=code, market_data={"AAPL": short, "MSFT": long}, warmup_bars_required=50
    )
    assert report.coverage_category is not CC.INSUFFICIENT_BARS


def test_or_with_unrestricted_leg_treats_warmup_as_universal() -> None:
    code = make_strategy('bar.symbol == "AAPL" or close > 100')
    aapl = flat_close_df(100.0, n=10)
    msft = flat_close_df(200.0, n=100)
    report = run_indicator_probe(
        strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft}, warmup_bars_required=50
    )
    assert report.coverage_category is not CC.INSUFFICIENT_BARS


def test_denylist_excludes_symbol_from_warmup_scoping() -> None:
    aapl = flat_close_df(200.0, n=200)
    msft = flat_close_df(50.0, n=50)
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL":
                    return
                if close > sma(close, 150):
                    pass
    """)
    report = run_indicator_probe(
        strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft}, warmup_bars_required=150
    )
    assert report.coverage_category is CC.INSUFFICIENT_BARS


# ────────────────────────────────────────────────────────────────────────
# § 11  Symbol-gate tests (multi-symbol market data)
# ────────────────────────────────────────────────────────────────────────


def test_symbol_gate_restricts_evaluation() -> None:
    code = make_strategy('bar.symbol == "AAPL" and close > 1000')
    aapl = flat_ohlcv(n=50)
    msft = flat_close_df(1500.0, n=50)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert "[AAPL]" in report.subconditions[0].label


def test_symbol_gated_duplicates_remain_distinct() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" and close > 50:
                    pass
                if bar.symbol == "MSFT" and close > 50:
                    pass
    """)
    aapl = flat_ohlcv(n=30)
    msft = flat_close_df(25.0)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 2
    aapl_row = next(sc for sc in report.subconditions if "[AAPL]" in sc.label)
    msft_row = next(sc for sc in report.subconditions if "[MSFT]" in sc.label)
    assert aapl_row.hit_count > 0
    assert msft_row.hit_count == 0


def test_symbol_gated_hit_rate_uses_matching_bars() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" and close > 50:
                    pass
                if bar.symbol == "MSFT" and close > 50:
                    pass
    """)
    aapl = flat_ohlcv(n=30)
    msft = flat_close_df(75.0)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.COVERAGE_OK
    for sc in report.subconditions:
        assert sc.hit_rate == 1.0


def test_contradictory_symbol_gates_drop_group() -> None:
    code = make_strategy('bar.symbol == "AAPL" and bar.symbol == "MSFT" and close > 0')
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": flat_ohlcv(n=20), "MSFT": flat_close_df(50.0, n=20)},
    )
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE
    assert report.subconditions == []


def test_compound_or_legs_are_recognised() -> None:
    code = make_strategy("(close > 50 and volume > 0) or (close < -10 and volume > 0)")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.COVERAGE_OK
    labels = [sc.label for sc in report.subconditions]
    assert any("close > 50" in lbl and "volume > 0" in lbl for lbl in labels)


def test_compound_or_legs_all_zero_flag_too_restrictive() -> None:
    code = make_strategy("(close > 1000 and volume > 0) or (close < -10 and volume > 0)")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_compound_or_leg_preserves_symbol_gate() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if (bar.symbol == "AAPL" and close > 1000) or \\
                        (bar.symbol == "MSFT" and close < 50):
                    pass
    """)
    aapl = flat_ohlcv(n=30)
    msft = flat_close_df(1500.0)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


# ────────────────────────────────────────────────────────────────────────
# § 12  Symbol-gate string-constant resolution (parametrised)
# ────────────────────────────────────────────────────────────────────────

_SYMBOL_CONST_CASES = [
    pytest.param(
        'TARGET_SYMBOL = "BBB"\n\nclass Strategy:\n    def on_bar(self, ctx, bar):\n'
        "        if bar.symbol == TARGET_SYMBOL and close > 100:\n            pass",
        {"BBB": 50.0, "AAA": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        None,
        id="named-string-module",
    ),
    pytest.param(
        'class Strategy:\n    def __init__(self):\n        self.TARGET = "BBB"\n\n'
        "    def on_bar(self, ctx, bar):\n        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"BBB": 50.0, "AAA": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        None,
        id="self-attr-string",
    ),
    pytest.param(
        'TARGET = "AAPL"\n\nclass Strategy:\n    TARGET = "MSFT"\n\n'
        "    def on_bar(self, ctx, bar):\n        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"MSFT": 200.0, "AAPL": 50.0},
        CC.COVERAGE_OK,
        "[MSFT]",
        id="class-overrides-module",
    ),
    pytest.param(
        'TARGET = "AAPL"\n\nclass Strategy:\n    TARGET = "MSFT"\n\n'
        "    def on_bar(self, ctx, bar):\n        if bar.symbol == TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="bare-name-resolves-module",
    ),
    pytest.param(
        'TARGET = "AAPL"\n\nclass Strategy:\n    def __init__(self):\n        self.TARGET = TARGET\n\n'
        "    def on_bar(self, ctx, bar):\n        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="self-attr-alias-module",
    ),
    pytest.param(
        'TARGET = "AAPL"\n\nclass Strategy:\n    TARGET = TARGET\n\n'
        "    def on_bar(self, ctx, bar):\n        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="class-body-alias-module",
    ),
]


@pytest.mark.parametrize(("code_text", "market", "expected", "label_contains"), _SYMBOL_CONST_CASES)
def test_symbol_gate_string_constant(
    code_text: str,
    market: dict,
    expected: CoverageCategory,
    label_contains: str | None,
) -> None:
    code = textwrap.dedent(code_text)
    md = {sym: flat_close_df(val) for sym, val in market.items()}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category is expected
    if label_contains:
        assert label_contains in report.subconditions[0].label


# ────────────────────────────────────────────────────────────────────────
# § 13  Constructor guard resolution (parametrised)
# ────────────────────────────────────────────────────────────────────────

_CTOR_GUARD_CASES = [
    pytest.param(
        'class Strategy:\n    TARGET = "MSFT"\n\n    def __init__(self):\n        if False:\n'
        '            self.TARGET = "AAPL"\n\n    def on_bar(self, ctx, bar):\n'
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"MSFT": 200.0, "AAPL": 50.0},
        CC.COVERAGE_OK,
        "[MSFT]",
        id="if-false-skipped",
    ),
    pytest.param(
        "class Strategy:\n    def __init__(self):\n        if True:\n"
        '            self.TARGET = "AAPL"\n\n    def on_bar(self, ctx, bar):\n'
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="if-true-recorded",
    ),
    pytest.param(
        "from contextlib import nullcontext\n\nclass Strategy:\n    def __init__(self):\n"
        '        with nullcontext():\n            self.TARGET = "AAPL"\n\n'
        "    def on_bar(self, ctx, bar):\n"
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="with-block-recorded",
    ),
    pytest.param(
        'class Strategy:\n    TARGET = "MSFT"\n\n    def __init__(self, some_flag=False):\n'
        '        if some_flag:\n            self.TARGET = "AAPL"\n\n'
        "    def on_bar(self, ctx, bar):\n"
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"MSFT": 200.0, "AAPL": 50.0},
        CC.COVERAGE_OK,
        "[MSFT]",
        id="unknown-guard-skipped",
    ),
    pytest.param(
        "class Strategy:\n    def __init__(self, enabled=True):\n        if enabled:\n"
        '            self.TARGET = "AAPL"\n\n    def on_bar(self, ctx, bar):\n'
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="default-true-param",
    ),
    pytest.param(
        'class Strategy:\n    TARGET = "MSFT"\n\n    def __init__(self, enabled=False):\n'
        '        if enabled:\n            self.TARGET = "AAPL"\n\n'
        "    def on_bar(self, ctx, bar):\n"
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"MSFT": 200.0, "AAPL": 50.0},
        CC.COVERAGE_OK,
        "[MSFT]",
        id="default-false-param",
    ),
    pytest.param(
        "class Strategy:\n    def __init__(self, disabled=False):\n        if not disabled:\n"
        '            self.TARGET = "AAPL"\n\n    def on_bar(self, ctx, bar):\n'
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="negated-default-false",
    ),
    pytest.param(
        'class Strategy:\n    TARGET = "MSFT"\n\n    def __init__(self):\n'
        '        TARGET = "AAPL"\n        _ = TARGET\n\n    def on_bar(self, ctx, bar):\n'
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"MSFT": 200.0, "AAPL": 50.0},
        CC.COVERAGE_OK,
        "[MSFT]",
        id="local-not-instance-attr",
    ),
    pytest.param(
        'class Strategy:\n    if True:\n        TARGET = "AAPL"\n\n'
        "    def on_bar(self, ctx, bar):\n"
        "        if bar.symbol == self.TARGET and close > 100:\n            pass",
        {"AAPL": 50.0, "MSFT": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        "[AAPL]",
        id="class-body-compound",
    ),
]


@pytest.mark.parametrize(("code_text", "market", "expected", "label_contains"), _CTOR_GUARD_CASES)
def test_constructor_guard_resolution(
    code_text: str,
    market: dict,
    expected: CoverageCategory,
    label_contains: str | None,
) -> None:
    code = textwrap.dedent(code_text)
    md = {sym: flat_close_df(val) for sym, val in market.items()}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category is expected
    if label_contains:
        assert label_contains in report.subconditions[0].label


# ────────────────────────────────────────────────────────────────────────
# § 14  Early-return symbol guards
# ────────────────────────────────────────────────────────────────────────

_EARLY_RETURN_CASES = [
    pytest.param(
        'class Strategy:\n    def on_bar(self, ctx, bar):\n        if bar.symbol != "BBB":\n'
        "            return\n        if close > 100:\n            pass",
        {"BBB": 50.0, "AAA": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        id="neq-guard",
    ),
    pytest.param(
        "class Strategy:\n    def on_bar(self, ctx, bar):\n"
        '        if bar.symbol not in ("BBB", "CCC"):\n'
        "            return\n        if close > 100:\n            pass",
        {"BBB": 50.0, "CCC": 50.0, "AAA": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        id="not-in-guard",
    ),
    pytest.param(
        'TARGET_SYMBOL = "BBB"\n\nclass Strategy:\n    def on_bar(self, ctx, bar):\n'
        "        if bar.symbol != TARGET_SYMBOL:\n            return\n"
        "        if close > 100:\n            pass",
        {"BBB": 50.0, "AAA": 200.0},
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
        id="neq-named-constant",
    ),
]


@pytest.mark.parametrize(("code_text", "market", "expected"), _EARLY_RETURN_CASES)
def test_early_return_symbol_guard(
    code_text: str, market: dict, expected: CoverageCategory
) -> None:
    code = textwrap.dedent(code_text)
    md = {sym: flat_close_df(val) for sym, val in market.items()}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category is expected


# ────────────────────────────────────────────────────────────────────────
# § 15  Denylist (positive eq/in return) guards
# ────────────────────────────────────────────────────────────────────────


def test_positive_symbol_eq_return_guard_is_denylist() -> None:
    n = 30
    aapl = flat_close_df(200.0, n=n)
    msft = flat_close_df(50.0, n=n)
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL":
                    return
                if close > 100:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_positive_symbol_in_return_guard_is_denylist() -> None:
    n = 30
    aapl = flat_close_df(200.0, n=n)
    goog = flat_close_df(200.0, n=n)
    msft = flat_close_df(50.0, n=n)
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol in ("AAPL", "GOOG"):
                    return
                if close > 100:
                    pass
    """)
    report = run_indicator_probe(
        strategy_code=code, market_data={"AAPL": aapl, "GOOG": goog, "MSFT": msft}
    )
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_negative_symbol_return_guard_unchanged() -> None:
    aapl = flat_close_df(200.0)
    msft = flat_close_df(50.0)
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol != "AAPL":
                    return
                if close > 100:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.COVERAGE_OK


# ────────────────────────────────────────────────────────────────────────
# § 16  OR-with-unknown-leg / conjunction tests on custom data
# ────────────────────────────────────────────────────────────────────────


def test_or_group_with_unknown_leg_suppresses_conjunction_never_true() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 100:
                    if close < 50 or self.custom_ok(bar):
                        pass
    """)
    report = run_indicator_probe(
        strategy_code=code, market_data={"AAPL": flat_close_df(200.0, n=20)}
    )
    assert report.coverage_category is not CC.CONJUNCTION_NEVER_TRUE


def test_or_group_with_ancestor_disjoint_flags_conjunction() -> None:
    n = 20
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = np.array([200.0] * 10 + [20.0] * 10)
    volume = np.array([0.0] * 10 + [10_000.0] * 10)
    df = pd.DataFrame(
        {"open": close, "high": close + 1.0, "low": close - 1.0, "close": close, "volume": volume},
        index=idx,
    )
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 100:
                    if close < 50 or volume > 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.CONJUNCTION_NEVER_TRUE


def test_or_group_with_ancestor_overlap_returns_ok() -> None:
    df = flat_close_df(200.0, n=20)
    df["volume"] = 10_000.0
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 100:
                    if close < 50 or volume > 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK


def test_plain_or_with_disjoint_legs_and_unknown_is_ok() -> None:
    code = make_strategy("close > 100 or close < 50 or self.custom_ok(bar)")
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_close_df(200.0)})
    assert report.coverage_category is CC.COVERAGE_OK


def test_top_level_or_preserves_standalone_symbol_gate() -> None:
    code = make_strategy('bar.symbol == "AAPL" or close > 100')
    df = flat_close_df(50.0)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK
    assert len(report.subconditions) == 2


def test_or_predicate_carries_into_nested_body() -> None:
    df = flat_close_df(200.0)
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 100 or close < 0:
                    if volume < 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_or_predicate_with_satisfying_body_stays_ok() -> None:
    df = flat_close_df(200.0)
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if close > 100 or close < 0:
                    if volume > 0:
                        pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert report.coverage_category is CC.COVERAGE_OK


# ────────────────────────────────────────────────────────────────────────
# § 17  OR symbol-allowlist inside AND, nested OR per-leg filter
# ────────────────────────────────────────────────────────────────────────


def test_or_symbol_allowlist_inside_and_predicate() -> None:
    code = make_strategy('(bar.symbol == "AAPL" or bar.symbol == "MSFT") and close > 100')
    md = {"AAPL": flat_close_df(50.0), "MSFT": flat_close_df(50.0), "TSLA": flat_close_df(200.0)}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category in {
        CC.CONJUNCTION_NEVER_TRUE,
        CC.INDICATOR_FILTER_TOO_RESTRICTIVE,
    }
    assert report.coverage_category is not CC.COVERAGE_OK


def test_or_allowlist_propagates_to_and_group() -> None:
    code = make_strategy('(bar.symbol == "AAPL" or bar.symbol == "MSFT") and close > 100')
    md = {"AAPL": flat_close_df(50.0), "MSFT": flat_close_df(50.0), "GOOG": flat_close_df(200.0)}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert "indicator_filter_zero_hits" in {b.reason for b in report.likely_blockers}


def test_nested_or_under_and_preserves_per_leg_symbol_filter() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, bar):
                if volume > 0 and (
                    (bar.symbol == "AAPL" and close > 1000)
                    or (bar.symbol == "MSFT" and close > 500)
                ):
                    pass
    """)
    md = {"AAPL": flat_close_df(200.0), "MSFT": flat_close_df(200.0), "TSLA": flat_close_df(2000.0)}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_positive_symbol_in_allowlist_gates_predicate() -> None:
    code = make_strategy('bar.symbol in ("AAPL", "MSFT") and close > 100')
    md = {"AAPL": flat_close_df(50.0), "MSFT": flat_close_df(50.0), "GOOG": flat_close_df(200.0)}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_partial_symbol_in_allowlist_does_not_gate() -> None:
    code = make_strategy('bar.symbol in ("AAPL", dynamic_lookup) and close > 100')
    df = flat_close_df(200.0)
    report = run_indicator_probe(strategy_code=code, market_data={"GOOG": df})
    assert report.coverage_category is CC.UNKNOWN_LOW_COVERAGE


def test_local_string_constant_resolves_in_symbol_gate() -> None:
    code = textwrap.dedent("""
        class Strategy:
            def on_bar(self, ctx, bar):
                target = "BBB"
                if bar.symbol == target and close > 100:
                    pass
    """)
    md = {"BBB": flat_close_df(50.0), "AAA": flat_close_df(200.0)}
    report = run_indicator_probe(strategy_code=code, market_data=md)
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_local_string_reassignment_clears_stale_symbol() -> None:
    code = textwrap.dedent("""
        class Strategy:
            def on_bar(self, ctx, bar):
                target = "BBB"
                target = self.lookup()
                if bar.symbol == target and close > 100:
                    pass
    """)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": flat_ohlcv()})
    assert report.coverage_category is not CC.COVERAGE_OK


# ────────────────────────────────────────────────────────────────────────
# § 18  Renamed bar parameter
# ────────────────────────────────────────────────────────────────────────


def test_renamed_bar_param_preserves_symbol_gate() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, candle):
                if candle.symbol == "AAPL" and candle.close > 150:
                    pass
    """)
    aapl = flat_ohlcv(n=50)
    msft = flat_close_df(200.0, n=50)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert "[AAPL]" in report.subconditions[0].label


def test_renamed_bar_param_preserves_early_return_guard() -> None:
    code = textwrap.dedent("""
        class S:
            def on_bar(self, ctx, candle):
                if candle.symbol != "AAPL":
                    return
                if close > 150:
                    pass
    """)
    aapl = flat_ohlcv(n=50)
    msft = flat_close_df(200.0, n=50)
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": aapl, "MSFT": msft})
    assert report.coverage_category is CC.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert "[AAPL]" in report.subconditions[0].label
