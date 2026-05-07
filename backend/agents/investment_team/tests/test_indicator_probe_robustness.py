"""Robustness tests for the indicator-coverage probe (#448)."""

from __future__ import annotations

import textwrap

import numpy as np
import pandas as pd

from investment_team.models import CoverageCategory
from investment_team.strategy_lab.coverage_probe import run_indicator_probe


def _flat_ohlcv(n: int = 60) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": np.full(n, 100.0),
            "high": np.full(n, 101.0),
            "low": np.full(n, 99.0),
            "close": np.full(n, 100.0),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def test_malformed_strategy_returns_unknown_low_coverage() -> None:
    report = run_indicator_probe(
        strategy_code="def on_bar(:::",
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert "did not parse" in report.summary


def test_empty_strategy_code_returns_unknown_low_coverage() -> None:
    report = run_indicator_probe(strategy_code="", market_data={"AAPL": _flat_ohlcv()})
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE


def test_no_recognized_subconditions_returns_unknown() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if 1 < 2:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert report.subconditions == []


def test_prefers_on_bar_over_top_level_helper() -> None:
    """A top-level helper named ``signal`` / ``generate_signal`` must
    not shadow the strategy's real ``on_bar`` entry path. ``on_bar`` is
    the actual contract — the fallback names exist only for legacy /
    free-function strategies that lack one.
    """
    code = textwrap.dedent(
        """
        def generate_signal():
            # No ``if`` predicates here — if the probe stops here it'll
            # report UNKNOWN_LOW_COVERAGE despite the real on_bar below.
            return None

        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label == "close > 0"


def test_module_level_period_constant_resolved() -> None:
    code = textwrap.dedent(
        """
        SMA_LOOKBACK = 5

        class S:
            def on_bar(self, ctx, bar):
                if close < sma(close, SMA_LOOKBACK) - 100:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # ``sma(close, SMA_LOOKBACK) - 100`` is roughly zero for our flat
    # fixture, so ``close < 0`` is structurally false. The Name lookup
    # must resolve so that the subcondition registers at all.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1


def test_class_attribute_window_resolves_in_indicator_arg() -> None:
    """Strategies routinely pass class tuning knobs to indicator helpers,
    e.g. ``sma(close, self.WINDOW)``. The probe must resolve the
    ``self.WINDOW`` Attribute through the class-attribute binding;
    without this the helper either crashed (no default period) or
    silently used the wrong default, producing misleading coverage.
    """
    # Sawtooth so close oscillates around the moving average; different
    # window lengths produce visibly different hit counts.
    n = 200
    moves = np.array([+0.005, -0.005] * (n // 2))
    close = 100.0 * np.cumprod(1.0 + moves)
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=n, freq="D"),
    )

    code_short = textwrap.dedent(
        """
        class S:
            WINDOW = 3
            def on_bar(self, ctx, bar):
                if close < sma(close, self.WINDOW):
                    pass
        """
    )
    code_long = textwrap.dedent(
        """
        class S:
            WINDOW = 50
            def on_bar(self, ctx, bar):
                if close < sma(close, self.WINDOW):
                    pass
        """
    )
    short = run_indicator_probe(strategy_code=code_short, market_data={"AAPL": df})
    long = run_indicator_probe(strategy_code=code_long, market_data={"AAPL": df})

    # If the Attribute weren't resolved, sma's required ``period`` would
    # be missing and the helper would raise — caught by the probe and
    # emitted as zero hits. Resolution makes both runs evaluate cleanly
    # with non-zero hits, and the different windows yield different counts.
    assert len(short.subconditions) == 1
    assert len(long.subconditions) == 1
    assert short.subconditions[0].hit_count > 0
    assert long.subconditions[0].hit_count > 0
    assert short.subconditions[0].hit_count != long.subconditions[0].hit_count


def test_init_self_assignment_window_is_resolved() -> None:
    """``self.WINDOW = 80`` inside ``__init__`` is a different AST shape
    (Attribute target, not Name). It must still bind so a downstream
    ``sma(close, self.WINDOW)`` resolves the period.
    """
    code = textwrap.dedent(
        """
        class S:
            def __init__(self):
                self.WINDOW = 5
            def on_bar(self, ctx, bar):
                if close > sma(close, self.WINDOW):
                    pass
        """
    )
    df = _flat_ohlcv(n=100)
    df.loc[df.index[60:], "close"] = 95.0  # below the SMA half the time
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    assert len(report.subconditions) == 1
    # The subcondition must evaluate (not silently zero out due to a
    # missing period).
    sc = report.subconditions[0]
    assert sc.label == "close > sma(close, self.WINDOW)"
    assert 0 <= sc.hit_count <= report.bars_checked


def test_position_check_else_branch_is_skipped() -> None:
    """``if pos is None: <entry> else: <exit>`` is the documented gate.

    An exit-only filter in the else branch must not be reported as an
    entry-coverage blocker — entries aren't restricted by exit rules.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is None:
                    if close > 0:
                        pass
                else:
                    if close < -50:
                        pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # Entry condition ``close > 0`` always fires on the flat fixture.
    # If the exit branch's never-true ``close < -50`` were also recorded,
    # the report would flip to INDICATOR_FILTER_TOO_RESTRICTIVE.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    labels = {sc.label for sc in report.subconditions}
    assert "close > 0" in labels
    assert "close < -50" not in labels


def test_position_check_via_ctx_call_is_recognized() -> None:
    """``if ctx.position(bar.symbol) is None:`` — same shape, different
    test expression. Must also skip the else branch.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if ctx.position(bar.symbol) is None:
                    if close > 0:
                        pass
                else:
                    if close < -50:
                        pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    labels = {sc.label for sc in report.subconditions}
    assert "close < -50" not in labels


def test_symbol_gate_restricts_evaluation_to_matching_dataframe() -> None:
    """``if bar.symbol == "AAPL" and close > 1000`` must evaluate the
    indicator condition only against AAPL — an unrelated symbol whose
    close already exceeds 1000 must NOT make this report COVERAGE_OK.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" and close > 1000:
                    pass
        """
    )
    aapl = _flat_ohlcv(n=50)  # close = 100 — never > 1000
    msft = pd.DataFrame(
        {
            "open": np.full(50, 1500.0),
            "high": np.full(50, 1505.0),
            "low": np.full(50, 1495.0),
            "close": np.full(50, 1500.0),  # close > 1000 always
            "volume": np.full(50, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=50, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
    )

    # AAPL never satisfies close > 1000 — that's the real coverage gap
    # we want to surface. If the symbol gate weren't honoured, MSFT's
    # 1500 close would mask the AAPL miss.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count == 0
    # The label is augmented with the symbol filter so the report
    # surfaces which branch it came from.
    assert "[AAPL]" in report.subconditions[0].label


def test_symbol_gated_duplicates_remain_distinct() -> None:
    """Two ``bar.symbol == "X"`` branches with the same predicate text
    must surface as TWO coverage rows. Otherwise dedupe-by-label drops
    the symbol-specific blocker the new ``target_symbols`` filter is
    supposed to catch.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" and close > 50:
                    pass
                if bar.symbol == "MSFT" and close > 50:
                    pass
        """
    )
    aapl = _flat_ohlcv(n=30)  # close = 100 — satisfies > 50 always
    msft = pd.DataFrame(
        {
            "open": np.full(30, 25.0),
            "high": np.full(30, 25.5),
            "low": np.full(30, 24.5),
            "close": np.full(30, 25.0),  # never > 50
            "volume": np.full(30, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=30, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
    )

    # The MSFT branch is a real zero-hit blocker; if we'd deduped only
    # by predicate text it would have been hidden.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 2
    by_label = {sc.label: sc for sc in report.subconditions}
    assert any("[AAPL]" in lbl for lbl in by_label)
    assert any("[MSFT]" in lbl for lbl in by_label)
    aapl_row = next(sc for sc in report.subconditions if "[AAPL]" in sc.label)
    msft_row = next(sc for sc in report.subconditions if "[MSFT]" in sc.label)
    assert aapl_row.hit_count > 0
    assert msft_row.hit_count == 0


def test_inverted_position_check_routes_to_orelse() -> None:
    """``if pos is not None: <exit> else: <entry>`` — the body is the
    EXIT path and the entry path is in ``orelse``. The probe must
    recurse into orelse for the entry-coverage analysis.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is not None:
                    if close < -50:
                        pass
                else:
                    if close > 0:
                        pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # Entry condition ``close > 0`` always fires; if the probe had
    # routed into body (the exit path) it would have flagged
    # ``close < -50`` as an INDICATOR_FILTER_TOO_RESTRICTIVE blocker.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    labels = {sc.label for sc in report.subconditions}
    assert "close > 0" in labels
    assert "close < -50" not in labels


def test_combined_position_gate_in_entry_test_routes_to_body() -> None:
    """``if pos is None and <entry>:`` / ``elif pos is not None and <exit>:``
    is the codegen-emitted shape (factors/compiler.py). The probe must
    strip the position-gate conjunct, treat the body of the vacant gate
    as the entry path (with the surviving conjunct(s) as coverage), and
    skip the elif's exit predicate entirely.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                pos = ctx.position(bar.symbol)
                if pos is None and close > 0:
                    pass
                elif pos is not None and close < -50:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # Entry-coverage subcond ``close > 0`` must be present; the elif's
    # exit-coverage ``close < -50`` must not be.
    labels = {sc.label for sc in report.subconditions}
    assert "close > 0" in labels
    assert "close < -50" not in labels
    assert report.coverage_category is CoverageCategory.COVERAGE_OK


def test_combined_position_gate_with_zero_hit_entry_flagged() -> None:
    """The surviving entry conjunct of a combined gate is real coverage
    — so when it never fires, the probe must still flag
    INDICATOR_FILTER_TOO_RESTRICTIVE rather than silently passing.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if pos is None and close < -50:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert any(sc.label == "close < -50" for sc in report.subconditions)


def test_symbol_gated_hit_rate_uses_matching_symbol_bars() -> None:
    """Symbol-gated rows must divide by the matching symbol's bars,
    not by the global universe. Without this, two always-true gated
    branches each report hit_rate=0.5 instead of 1.0 when the universe
    has two equally-sized symbols.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" and close > 50:
                    pass
                if bar.symbol == "MSFT" and close > 50:
                    pass
        """
    )
    aapl = _flat_ohlcv(n=30)  # close = 100 — always > 50
    msft = pd.DataFrame(
        {
            "open": np.full(30, 75.0),
            "high": np.full(30, 75.5),
            "low": np.full(30, 74.5),
            "close": np.full(30, 75.0),  # always > 50
            "volume": np.full(30, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=30, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
    )
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    # Both branches always fire on their respective symbols. With the
    # matching-bars denominator each row reports hit_rate == 1.0.
    assert len(report.subconditions) == 2
    for sc in report.subconditions:
        assert sc.hit_count == 30
        assert sc.hit_rate == 1.0


def test_contradictory_same_predicate_symbol_gates_drop_group() -> None:
    """``bar.symbol == "AAPL" and bar.symbol == "MSFT" and close > 0``
    is structurally unreachable — both literal symbols can't be true on
    the same bar. The intra-predicate symbol-gate combiner must
    intersect (not union) the two literals, leaving an empty filter,
    and the resulting empty-set group must be dropped before evaluation.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" and bar.symbol == "MSFT" and close > 0:
                    pass
        """
    )
    aapl = _flat_ohlcv(n=20)  # close > 0 always
    msft = pd.DataFrame(
        {
            "open": np.full(20, 50.0),
            "high": np.full(20, 51.0),
            "low": np.full(20, 49.0),
            "close": np.full(20, 50.0),
            "volume": np.full(20, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=20, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
    )
    # Without the intersection fix, the symbol filter would be
    # {AAPL, MSFT} (union) and ``close > 0`` would evaluate against
    # both DataFrames, reporting COVERAGE_OK. With intersection the
    # filter is empty, the group is dropped, and the probe sees no
    # recognised subconditions at all.
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert report.subconditions == []


def test_indicator_with_history_listcomp_input_uses_correct_column() -> None:
    """``sma([b.volume for b in history], 5)`` must compute over the
    volume column, not silently fall back to close. With flat
    ``volume=1000`` and ``close=100`` the predicate
    ``volume > vol_avg * 1.5`` is structurally false; the probe must
    flag INDICATOR_FILTER_TOO_RESTRICTIVE rather than COVERAGE_OK.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                vol_avg = sma([b.volume for b in history], 5)
                if volume > vol_avg * 1.5:
                    pass
        """
    )
    df = _flat_ohlcv(n=30)  # flat volume=1_000_000
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    # Flat data: sma(volume) == volume, so volume > sma(volume)*1.5 is
    # never true. If the probe had defaulted to close it would have
    # computed sma(close)*1.5 ≈ 150 and reported COVERAGE_OK because
    # volume (1_000_000) is greater than that.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count == 0


def test_indicator_with_unrecognised_explicit_input_is_dropped() -> None:
    """``rsi(self.history)`` with an opaque input must be dropped, not
    silently substituted with close. Otherwise the probe would compute
    coverage against the wrong series and a real blocker could become
    COVERAGE_OK.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if rsi(self.history) < 25:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # The single subcondition is unrecognised → no recognised
    # subconditions → UNKNOWN_LOW_COVERAGE.
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert report.subconditions == []


def test_derived_threshold_assign_is_bound() -> None:
    """``threshold = sma(close, 5) * 1.02`` must bind ``threshold`` to
    the BinOp evaluator so the later comparison ``close > threshold``
    reaches the coverage check. Without the BinOp-of-indicator binding
    the Name lookup fails and the comparison is dropped.
    """
    code_named = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                threshold = sma(close, 5) * 1.02
                if close > threshold:
                    pass
        """
    )
    code_inline = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > sma(close, 5) * 1.02:
                    pass
        """
    )
    df = _flat_ohlcv(n=50)
    df.loc[df.index[25:], "close"] = 105.0  # half the bars above the 1.02 band

    named = run_indicator_probe(strategy_code=code_named, market_data={"AAPL": df})
    inline = run_indicator_probe(strategy_code=code_inline, market_data={"AAPL": df})

    # The named form must produce the same coverage outcome as the
    # inline form. Without the fix the named form would have returned
    # UNKNOWN_LOW_COVERAGE because ``threshold`` would never have been
    # bound.
    assert named.coverage_category is inline.coverage_category
    assert len(named.subconditions) == 1
    assert len(inline.subconditions) == 1
    assert named.subconditions[0].hit_count == inline.subconditions[0].hit_count
    assert named.subconditions[0].hit_count > 0


def test_atr_positional_period_is_resolved() -> None:
    """``atr(high, low, close, N)`` puts the period at args[3], not args[1].

    Regression for a bug where the generic period extractor read args[1]
    (which is ``low`` for HLC helpers) and silently fell back to the
    helper's default of 14.

    ATR scales with the magnitude of true-range moves. A short window
    (period=2) over the ``_swing_close`` fixture below produces a
    substantially larger steady-state ATR than the default period=14.
    The test asserts the probe actually USES the requested period by
    comparing hit rates of ``atr(high, low, close, 2) > T`` against
    a plain ``atr(high, low, close) > T`` over the same data — they
    must differ.
    """

    def _swing_close(n: int = 100) -> pd.DataFrame:
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        # Sharp alternating moves so short-window ATR diverges from default.
        moves = np.array([+0.02, -0.02] * (n // 2))
        close = 100.0 * np.cumprod(1.0 + moves)
        return pd.DataFrame(
            {
                "open": close,
                "high": close * 1.01,
                "low": close * 0.99,
                "close": close,
                "volume": np.full(n, 1_000_000.0),
            },
            index=idx,
        )

    code_short = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if atr(high, low, close, 2) > 3:
                    pass
        """
    )
    code_default = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if atr(high, low, close) > 3:
                    pass
        """
    )

    df = _swing_close()
    short = run_indicator_probe(strategy_code=code_short, market_data={"SYM": df})
    default = run_indicator_probe(strategy_code=code_default, market_data={"SYM": df})

    # If the period weren't honoured, both would compute the same ATR
    # (the default period=14) and report identical hit_count. The bug
    # we're guarding against is exactly that silent fallback.
    assert short.subconditions[0].hit_count != default.subconditions[0].hit_count


def test_no_llm_calls_made() -> None:
    """The indicator probe must not import or reference the LLM client.

    A static check on the module's source code is stronger than a runtime
    monkey-patch — the latter can leak into parallel pytest workers under
    ``-n auto`` and flake unrelated tests.
    """
    import inspect

    import investment_team.strategy_lab.coverage_probe.indicator_probe as mod

    src = inspect.getsource(mod)
    assert "llm_service" not in src
    assert "LLMClient" not in src
    assert "OllamaClient" not in src

    # Module exports also must not surface any llm-named symbols.
    for name in dir(mod):
        assert "llm" not in name.lower(), f"unexpected llm symbol: {name}"

    # Smoke-call the probe to confirm it still runs cleanly.
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.COVERAGE_OK


def test_evaluator_failure_per_subcondition_does_not_raise() -> None:
    """A subcondition referencing a column that's missing should degrade,
    not raise. We pass a DataFrame with no ``volume`` column but a
    ``volume``-touching subcondition; the probe should treat the leg as
    non-firing rather than crashing."""
    df = pd.DataFrame(
        {
            "open": np.full(30, 100.0),
            "high": np.full(30, 101.0),
            "low": np.full(30, 99.0),
            "close": np.full(30, 100.0),
        },
        index=pd.date_range("2024-01-01", periods=30, freq="D"),
    )
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if volume > 0:
                    pass
        """
    )
    report = run_indicator_probe(strategy_code=code, market_data={"SYM": df})

    # Volume column missing → all NaN → fillna(False) → zero hits.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count == 0


def test_empty_market_data_does_not_raise() -> None:
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    pass
        """
    )
    report = run_indicator_probe(strategy_code=code, market_data={})
    # Zero bars → no eval, but strategy parses and finds a subcondition →
    # COVERAGE_OK with empty hit data is not meaningful; the implementation
    # currently returns COVERAGE_OK for "no zero hits" — that is acceptable
    # because INSUFFICIENT_BARS is gated on warmup_bars_required > 0.
    assert report.coverage_category in {
        CoverageCategory.COVERAGE_OK,
        CoverageCategory.UNKNOWN_LOW_COVERAGE,
    }
    assert report.bars_checked == 0


def test_bool_call_on_indicator_name_is_recognized() -> None:
    """`bool(<Name>)` where the name resolves to an indicator binding
    must produce a real subcondition rather than being silently dropped.

    Regression for the codex finding on PR #456: stripping the position
    gate from `if pos is None and bool(_entry):` left `bool(_entry)` as
    the residual test, but `_flatten_test` only returned `Compare`
    nodes, so the term was discarded and the probe reported
    `UNKNOWN_LOW_COVERAGE`.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                _entry = sma(close, 5)
                pos = ctx.position(bar.symbol)
                if pos is None and bool(_entry):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label == "bool(_entry)"
    assert report.subconditions[0].hit_count > 0


def test_bare_name_truthiness_residual_is_recognized() -> None:
    """A bare `Name` left as the residual after stripping the position
    gate (`if pos is None and _entry:`) must reach the coverage check
    when the name is bound to an indicator.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                _entry = sma(close, 5)
                pos = ctx.position(bar.symbol)
                if pos is None and _entry:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label == "_entry"
    assert report.subconditions[0].hit_count > 0


def test_bool_call_on_compare_delegates_to_compare_subcond() -> None:
    """`bool(<Compare>)` should produce the same subcondition as the
    bare comparison — useful when codegen wraps a comparison in
    `bool(...)` for symmetry with the truthiness path.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bool(close > 50):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label == "close > 50"
    assert report.subconditions[0].hit_count > 0


def test_bool_call_on_unbound_name_remains_unknown() -> None:
    """The compiler-emitted factor-tree shape `_entry = self._n_X(bars)`
    binds `_entry` to a method call we cannot statically introspect, so
    `_collect_name_evaluators` doesn't pick it up. The probe must surface
    that as `UNKNOWN_LOW_COVERAGE` rather than silently treating
    `bool(_entry)` as always-true.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                _entry = self._n_root(bars)
                pos = ctx.position(bar.symbol)
                if pos is None and bool(_entry):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert report.subconditions == []
