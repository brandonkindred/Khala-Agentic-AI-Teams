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


def test_or_predicate_with_one_firing_leg_is_coverage_ok() -> None:
    """``if close > 100 or rsi(close, 14) < 30:`` — even when one leg
    never fires, the OR is satisfied as long as the other does. Must
    classify ``COVERAGE_OK`` and surface both legs as coverage rows.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 50 or close > 1000:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},  # close=100 — > 50 always, > 1000 never
    )
    # The ``> 1000`` leg never fires but the OR is still satisfied via
    # the ``> 50`` leg. Must NOT classify INDICATOR_FILTER_TOO_RESTRICTIVE
    # — that would wrongly suggest the entry is blocked.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 2
    by_label = {sc.label: sc for sc in report.subconditions}
    assert by_label["close > 50"].hit_count == 60
    assert by_label["close > 1000"].hit_count == 0


def test_or_predicate_with_all_legs_zero_is_too_restrictive() -> None:
    """When every leg of the OR is zero-hit the disjunction never fires
    and the entry is genuinely blocked. Must classify
    INDICATOR_FILTER_TOO_RESTRICTIVE with an ``or_group_never_fires``
    blocker that lists all legs.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close < -10 or close > 1000:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.likely_blockers) == 1
    blocker = report.likely_blockers[0]
    assert blocker.reason == "or_group_never_fires"
    assert "close < -10" in blocker.evidence
    assert "close > 1000" in blocker.evidence
    assert " OR " in blocker.evidence


def test_or_under_ancestor_keeps_or_semantics() -> None:
    """``if close > 0: if close > 0 or close < 0:`` is reachable on
    positive prices because the inner OR's first leg always fires.
    The probe must keep OR semantics even when the OR is nested under
    an ancestor — the dead ``close < 0`` leg should not flag a
    blocker, since one firing leg satisfies the disjunction.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    if close > 0 or close < 0:
                        pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # Without the ancestor_count fix, the inner OR's ``close < 0``
    # leg was treated as an AND-required conjunct of the outer-plus-
    # inner group and reported as INDICATOR_FILTER_TOO_RESTRICTIVE.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    labels = {sc.label for sc in report.subconditions}
    # The dead leg still surfaces as a coverage row (so users see it),
    # but it isn't a blocker.
    assert "close > 0" in labels
    assert "close < 0" in labels


def test_or_under_ancestor_zero_hit_ancestor_still_blocks() -> None:
    """OR-with-ancestor: a zero-hit ancestor is still an AND-required
    blocker. ``if close > 1000: if close > 0 or close < 0:`` — the
    outer ancestor never fires on a flat fixture, so the predicate is
    blocked regardless of the inner OR's coverage.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 1000:
                    if close > 0 or close < 0:
                        pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    # The ancestor leg gets flagged via the AND zero-hit rule even
    # though the group's combinator is OR.
    blocker_reasons = [b.reason for b in report.likely_blockers]
    assert "indicator_filter_zero_hits" in blocker_reasons
    blocker_evidence = [b.evidence for b in report.likely_blockers]
    assert any("close > 1000" in e for e in blocker_evidence)


def test_named_series_arg_resolves_via_binding() -> None:
    """``closes = [b.close for b in history]; rsi(closes, 14) < 25``
    must evaluate the indicator over the bound series rather than
    being dropped because ``closes`` isn't directly an OHLCV column.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                closes = [b.close for b in history]
                if rsi(closes, 14) < -50:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # The indicator was previously dropped because args[0] was a
    # ``Name`` not directly recognisable as an OHLCV column. With the
    # name-binding resolution it's evaluated normally; rsi < -50 is
    # never true so the predicate is correctly classified as
    # INDICATOR_FILTER_TOO_RESTRICTIVE rather than UNKNOWN_LOW_COVERAGE.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1


def test_warmup_uses_per_symbol_max_history() -> None:
    """Warmup is a per-symbol time-series property. Two 100-bar
    DataFrames with ``warmup_bars_required=150`` must classify as
    INSUFFICIENT_BARS — no individual symbol has enough history to
    compute a 150-period indicator. The previous aggregate-row guard
    (sum=200 ≥ 150) wrongly let the probe through and the all-NaN
    indicators surfaced as INDICATOR_FILTER_TOO_RESTRICTIVE.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > sma(close, 150):
                    pass
        """
    )
    aapl = _flat_ohlcv(n=100)
    msft = _flat_ohlcv(n=100)
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
        warmup_bars_required=150,
    )
    assert report.coverage_category is CoverageCategory.INSUFFICIENT_BARS
    assert report.bars_checked == 200  # aggregate count preserved on the model
    assert report.warmup_bars_required == 150
    assert report.likely_blockers
    assert report.likely_blockers[0].reason == "insufficient_bars"


def test_warmup_passes_when_one_symbol_has_enough_history() -> None:
    """When at least one symbol has enough bars, the probe proceeds.
    Underwarmed symbols' indicator NaNs flow through ``fillna(False)``
    so they contribute zero hits but don't falsely block the report.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0:
                    pass
        """
    )
    aapl = _flat_ohlcv(n=200)
    msft = _flat_ohlcv(n=50)
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
        warmup_bars_required=150,
    )
    # AAPL satisfies warmup; MSFT's 50 bars contribute too.
    # ``close > 0`` fires on every bar, so COVERAGE_OK.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK


def test_compound_or_legs_are_recognised() -> None:
    """``(A and B) or (C and D)`` — each OR leg is a BoolOp(And, ...),
    not a Compare. Each compound leg must be built as a single subcond
    whose evaluator is the bar-wise AND of its inner conjuncts.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if (close > 50 and volume > 0) or (close < -10 and volume > 0):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # Left leg fires on every bar (close=100 > 50, volume > 0); right
    # leg never fires. The OR is satisfied via the left leg.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    # Both compound legs surface as coverage rows so users see the
    # dead alternative.
    labels = [sc.label for sc in report.subconditions]
    assert any("close > 50" in lbl and "volume > 0" in lbl for lbl in labels)
    assert any("close < -10" in lbl and "volume > 0" in lbl for lbl in labels)


def test_compound_or_legs_all_zero_flag_too_restrictive() -> None:
    """If every compound OR leg's bar-wise AND is empty, the disjunction
    never fires and the predicate is genuinely blocked.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if (close > 1000 and volume > 0) or (close < -10 and volume > 0):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    blocker_reasons = [b.reason for b in report.likely_blockers]
    assert "or_group_never_fires" in blocker_reasons


def test_compound_or_leg_preserves_symbol_gate() -> None:
    """``(bar.symbol == "AAPL" and close > 1000) or
    (bar.symbol == "MSFT" and close < 50)`` — each compound OR leg's
    symbol gate must constrain THAT leg's evaluation. Otherwise the
    AAPL leg's ``close > 1000`` mask runs against MSFT's data (where
    close=1500 always), and the OR appears to fire even though
    neither symbol-specific branch is actually true.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if (bar.symbol == "AAPL" and close > 1000) or \\
                        (bar.symbol == "MSFT" and close < 50):
                    pass
        """
    )
    aapl = _flat_ohlcv(n=30)  # close=100 — never > 1000
    msft = pd.DataFrame(
        {
            "open": np.full(30, 1500.0),
            "high": np.full(30, 1505.0),
            "low": np.full(30, 1495.0),
            "close": np.full(30, 1500.0),  # never < 50
            "volume": np.full(30, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=30, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
    )
    # Without the per-leg symbol filter, the AAPL leg's close-gt-1000
    # mask would fire on MSFT (close=1500), making the OR appear
    # satisfied. With the filter neither leg can fire on its gated
    # symbol → INDICATOR_FILTER_TOO_RESTRICTIVE.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    blocker_reasons = [b.reason for b in report.likely_blockers]
    assert "or_group_never_fires" in blocker_reasons
    # Leg labels in the report carry their per-leg symbol filter so
    # users can tell the AAPL branch from the MSFT branch.
    labels = [sc.label for sc in report.subconditions]
    assert any("[AAPL]" in lbl for lbl in labels)
    assert any("[MSFT]" in lbl for lbl in labels)


def test_tuple_unpacked_indicator_outputs_bind() -> None:
    """``upper, mid, lower = bollinger_bands(closes, 20)`` followed by
    ``if bar.close > upper:`` must bind ``upper`` to the first output
    of the helper so the comparison can be evaluated. Without the
    tuple-unpack path the binding was missed and the report dropped
    to UNKNOWN_LOW_COVERAGE.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                closes = [b.close for b in history]
                upper, mid, lower = bollinger_bands(closes, 20)
                if close > upper:
                    pass
        """
    )
    # Compare two fixtures: one with shocks that periodically push
    # close above the upper band (hit_count > 0) versus a flat fixture
    # where close == upper after warmup (hit_count = 0). If ``upper``
    # weren't bound to the upper-band series both runs would land in
    # the same UNKNOWN_LOW_COVERAGE bucket — distinct hit counts prove
    # the binding flowed through.
    n = 100
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    closes_shock = np.full(n, 100.0)
    closes_shock[::10] = 130.0  # periodic spikes well above any 20-bar SMA + 2σ
    df_shock = pd.DataFrame(
        {
            "open": closes_shock,
            "high": closes_shock * 1.005,
            "low": closes_shock * 0.995,
            "close": closes_shock,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df_shock})
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label == "close > upper"
    # Spikes above the band → non-zero hits, proving ``upper`` was
    # bound to the actual upper-band series rather than dropped.
    assert report.subconditions[0].hit_count > 0
    assert report.coverage_category is CoverageCategory.COVERAGE_OK


def test_tuple_unpacked_stochastic_binds_hlc_signature() -> None:
    """Stochastic is HLC-typed (returns %K, %D from h/l/c inputs).
    ``k, d = stochastic(high, low, close)`` followed by ``if k < 20:``
    must bind both names to the corresponding output series.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                k, d = stochastic(high, low, close)
                if k < 20:
                    pass
        """
    )
    n = 60
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    # Bottoming-out swing so %K dips below 20 part of the time.
    moves = np.array([-0.005] * 30 + [+0.005] * 30)
    closes = 100.0 * np.cumprod(1.0 + moves)
    df = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})
    assert len(report.subconditions) == 1
    assert report.subconditions[0].label == "k < 20"
    # Whether COVERAGE_OK or INDICATOR_FILTER_TOO_RESTRICTIVE depends
    # on the exact %K trajectory — we only assert the binding fired
    # (the subcond was recognised and evaluated rather than dropped).
    assert report.coverage_category in {
        CoverageCategory.COVERAGE_OK,
        CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
    }


def test_nested_or_under_and_is_evaluated() -> None:
    """``if close > 0 and (volume < 0 or close < -1):`` — the inner OR
    is one term of the outer AND. Without nested-OR handling the inner
    disjunction was dropped and the AND classification was based only
    on the surviving Compare conjunct (`close > 0` always fires →
    false COVERAGE_OK). Now the inner OR is built as a compound
    AND-conjunct whose mask is the bar-wise OR of its legs; both legs
    fail on flat data so the AND classifies INDICATOR_FILTER_TOO_RESTRICTIVE.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and (volume < 0 or close < -1):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    # Both AND conjuncts surface as coverage rows: the bare ``close > 0``
    # and the synthetic ``volume < 0 or close < -1`` compound.
    labels = [sc.label for sc in report.subconditions]
    assert "close > 0" in labels
    assert any("volume < 0" in lbl and "close < -1" in lbl for lbl in labels)


def test_nested_or_under_and_with_one_leg_firing_is_coverage_ok() -> None:
    """``if close > 0 and (volume > 0 or close < -1):`` — the inner OR
    fires via ``volume > 0`` on every bar (flat fixture has volume=1M).
    Both AND legs satisfied → COVERAGE_OK.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 0 and (volume > 0 or close < -1):
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.COVERAGE_OK


def test_reassigned_local_uses_latest_binding() -> None:
    """Python uses the latest local assignment. ``threshold = sma(close,
    5) - 1; threshold = sma(close, 5) + 1000; if close > threshold:``
    on flat data is impossible (close=100, threshold≈1100), but the
    previous ``setdefault`` made the first stale binding stick and the
    probe wrongly reported COVERAGE_OK from ``threshold=99``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                threshold = sma(close, 5) - 1
                threshold = sma(close, 5) + 1000
                if close > threshold:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    # close=100, second threshold = sma(close, 5) + 1000 ≈ 1100 — never
    # satisfied. Must classify INDICATOR_FILTER_TOO_RESTRICTIVE.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count == 0


def test_or_predicate_with_three_legs_recognised() -> None:
    """OR predicates with more than two legs must each surface as a
    coverage row.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > 50 or close > 1000 or close < -10:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 3


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


def test_cached_compare_entry_predicate_binds_through_bool() -> None:
    """``_entry = close > sma(close, 5)`` followed by ``if pos is None
    and bool(_entry):`` is the documented hand-written shape that caches
    a comparison rule into a local. The probe must bind ``_entry`` to
    the comparison's evaluator so ``bool(_entry)`` resolves and the
    probe diagnoses the cached rule rather than reporting
    UNKNOWN_LOW_COVERAGE.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                _entry = close > sma(close, 5)
                pos = ctx.position(bar.symbol)
                if pos is None and bool(_entry):
                    pass
        """
    )
    df = _flat_ohlcv(n=50)
    df.loc[df.index[25:], "close"] = 105.0  # rising step → close > sma half the bars
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    assert len(report.subconditions) == 1
    sc = report.subconditions[0]
    # The label is the bool(_entry) wrapper; the underlying comparison
    # evaluator must run, so hits should be > 0 on this fixture.
    assert sc.hit_count > 0
    assert report.coverage_category is CoverageCategory.COVERAGE_OK


def test_nested_or_under_and_preserves_per_leg_symbol_filter() -> None:
    """Per-leg ``bar.symbol == "X"`` gates inside an OR wrapper must
    survive into the aggregator, otherwise an unrelated symbol's data
    can satisfy a leg restricted to AAPL/MSFT and falsely flip the OR.

    Predicate:
        ``volume > 0 and ((bar.symbol == "AAPL" and close > 1000)
                          or (bar.symbol == "MSFT" and close > 500))``

    Data: AAPL/MSFT close=200 (never exceed their thresholds), TSLA
    close=2000 (would satisfy ``close > 1000`` if its symbol gate is
    dropped). Without the per-leg filter the OR wrapper folds the AAPL
    leg's mask over every DataFrame, TSLA bars satisfy ``close > 1000``,
    and the AND group reports ``COVERAGE_OK``. With the fix the leg's
    ``target_symbols`` survives, TSLA contributes False to both legs,
    and the OR is empty so the AND group flags
    ``INDICATOR_FILTER_TOO_RESTRICTIVE``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if volume > 0 and (
                    (bar.symbol == "AAPL" and close > 1000)
                    or (bar.symbol == "MSFT" and close > 500)
                ):
                    pass
        """
    )

    def _df(close_value: float) -> pd.DataFrame:
        n = 30
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {
                "open": np.full(n, close_value),
                "high": np.full(n, close_value + 1.0),
                "low": np.full(n, close_value - 1.0),
                "close": np.full(n, close_value),
                "volume": np.full(n, 1_000_000.0),
            },
            index=idx,
        )

    report = run_indicator_probe(
        strategy_code=code,
        market_data={
            "AAPL": _df(200.0),
            "MSFT": _df(200.0),
            "TSLA": _df(2000.0),
        },
    )

    # Both gated legs are unreachable: AAPL never exceeds 1000, MSFT
    # never exceeds 500, and TSLA's 2000 close must NOT satisfy either
    # leg because its symbol isn't in the per-leg gate.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE


def test_function_local_period_shadows_outer_scope() -> None:
    """A function-local ``WINDOW = 5`` must override a module/class-level
    ``WINDOW = 200`` when resolving ``sma(close, WINDOW)``. Python uses
    the local value at runtime; the probe must too. Without the fix
    ``setdefault`` keeps the first (outer) binding and the probe
    evaluates against ``sma(close, 200)`` over a 30-bar fixture, which
    has no warmup-complete bars and yields zero hits — falsely flagging
    ``INDICATOR_FILTER_TOO_RESTRICTIVE``.
    """
    code = textwrap.dedent(
        """
        WINDOW = 200

        class S:
            def on_bar(self, ctx, bar):
                WINDOW = 5
                if close > sma(close, WINDOW):
                    pass
        """
    )
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves = [-0.005] * 8 + [+0.005] * 8 + [-0.005] * 7 + [+0.005] * 7
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    # With WINDOW=5 the SMA has plenty of warm-up-complete bars and the
    # comparison fires partially across the swing. With the bug
    # (WINDOW=200) every bar would be NaN → zero hits.
    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count > 0


def test_exit_branch_reassignment_does_not_shadow_entry_binding() -> None:
    """Exit-branch reassignments inside a position-check ``orelse`` must
    not overwrite an entry-branch binding. The recent overwrite-not-
    setdefault fix applied to all reassignments in the on_bar walk;
    without scoping it to the entry control-flow path, the codegen
    pattern below evaluates the entry comparison against the exit
    branch's 200-period MA and falsely flags
    ``INDICATOR_FILTER_TOO_RESTRICTIVE``.

    Strategy:
        ma = sma(close, 5)
        if pos is None:
            if close > ma: enter
        else:
            ma = sma(close, 200)   # exit-only reassignment

    On a 30-bar swing fixture the entry's 5-period MA has plenty of
    warmup-complete bars and ``close > ma`` partially fires. With the
    bug the exit binding (200-period) wins, every bar is NaN, and
    hits=0 → ``INDICATOR_FILTER_TOO_RESTRICTIVE``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                ma = sma(close, 5)
                pos = ctx.position(bar.symbol)
                if pos is None:
                    if close > ma:
                        pass
                else:
                    ma = sma(close, 200)
        """
    )
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves = [-0.005] * 8 + [+0.005] * 8 + [-0.005] * 7 + [+0.005] * 7
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count > 0


def test_warmup_check_restricted_to_gated_symbols() -> None:
    """Warmup denominator must shrink to the symbols that can satisfy a
    symbol-gated predicate. An unrelated long DataFrame in the universe
    must not rescue the warmup check when the gated symbol is too short.

    Strategy gates entry to AAPL only:
        if bar.symbol == "AAPL" and close > sma(close, 50): enter

    Universe: AAPL with 10 bars, MSFT with 100 bars,
    warmup_bars_required=50.

    With the bug the global ``max_per_symbol_bars=100`` (from MSFT)
    passes the warmup check; AAPL's SMA(50) is all-NaN over its 10
    bars, hits=0, and the probe wrongly reports
    ``INDICATOR_FILTER_TOO_RESTRICTIVE``. With the fix the warmup
    denominator is restricted to AAPL → 10 bars < 50 →
    ``INSUFFICIENT_BARS``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" and close > sma(close, 50):
                    pass
        """
    )
    aapl = pd.DataFrame(
        {
            "open": np.full(10, 100.0),
            "high": np.full(10, 101.0),
            "low": np.full(10, 99.0),
            "close": np.full(10, 100.0),
            "volume": np.full(10, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=10, freq="D"),
    )
    msft = pd.DataFrame(
        {
            "open": np.full(100, 200.0),
            "high": np.full(100, 201.0),
            "low": np.full(100, 199.0),
            "close": np.full(100, 200.0),
            "volume": np.full(100, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=100, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
        warmup_bars_required=50,
    )

    assert report.coverage_category is CoverageCategory.INSUFFICIENT_BARS
    assert len(report.likely_blockers) == 1
    blocker = report.likely_blockers[0]
    assert blocker.reason == "insufficient_bars"
    assert "AAPL" in (blocker.evidence or "")


def test_warmup_check_unaffected_when_any_group_is_universal() -> None:
    """If any extracted group has no symbol filter, the warmup check
    falls back to the full universe — a universal group can satisfy
    on any fetched symbol, so any sufficiently long DataFrame meets it.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if close > sma(close, 50):
                    pass
        """
    )
    short = pd.DataFrame(
        {
            "open": np.full(10, 100.0),
            "high": np.full(10, 101.0),
            "low": np.full(10, 99.0),
            "close": np.full(10, 100.0),
            "volume": np.full(10, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=10, freq="D"),
    )
    long = pd.DataFrame(
        {
            "open": np.full(100, 100.0),
            "high": np.full(100, 101.0),
            "low": np.full(100, 99.0),
            "close": np.full(100, 100.0),
            "volume": np.full(100, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=100, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": short, "MSFT": long},
        warmup_bars_required=50,
    )

    # Predicate is unrestricted by symbol; MSFT's 100 bars satisfy
    # warmup so we shouldn't short-circuit on INSUFFICIENT_BARS.
    assert report.coverage_category is not CoverageCategory.INSUFFICIENT_BARS


def test_or_symbol_allowlist_inside_and_predicate() -> None:
    """A symbol allowlist written as an OR inside a larger AND must
    still gate the indicator side.

    Predicate:
        ``if (bar.symbol == "AAPL" or bar.symbol == "MSFT") and close > 100:``

    Universe: AAPL with close=50 (never > 100), MSFT with close=50,
    TSLA with close=200 (would satisfy ``close > 100`` if the symbol
    allowlist is dropped).

    Without the fix, each ``bar.symbol == X`` Compare leg is sent to
    ``_build_subcond`` and rejected (no data-dependent operand), so
    the OR collapses to nothing and the AND keeps only ``close > 100``
    evaluated against every symbol — TSLA bars satisfy and the probe
    falsely reports ``COVERAGE_OK``. With the fix,
    ``_build_compound_or_subcond`` captures the symbol gates as
    per-leg ``target_symbols``, the outer OR subcond is gated to
    ``{AAPL, MSFT}``, and TSLA bars contribute False so the AND
    correctly flags the predicate as unreachable
    (``CONJUNCTION_NEVER_TRUE`` or ``INDICATOR_FILTER_TOO_RESTRICTIVE``
    — either is correct; the bug surfaces as ``COVERAGE_OK``).
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if (bar.symbol == "AAPL" or bar.symbol == "MSFT") and close > 100:
                    pass
        """
    )

    def _df(close_value: float) -> pd.DataFrame:
        n = 30
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {
                "open": np.full(n, close_value),
                "high": np.full(n, close_value + 1.0),
                "low": np.full(n, close_value - 1.0),
                "close": np.full(n, close_value),
                "volume": np.full(n, 1_000_000.0),
            },
            index=idx,
        )

    report = run_indicator_probe(
        strategy_code=code,
        market_data={
            "AAPL": _df(50.0),
            "MSFT": _df(50.0),
            "TSLA": _df(200.0),
        },
    )

    # The allowlisted symbols never satisfy ``close > 100``; TSLA's
    # close=200 must NOT be allowed to fire because TSLA isn't in the
    # OR allowlist. The bug surfaces as COVERAGE_OK (TSLA satisfies);
    # both CONJUNCTION_NEVER_TRUE and INDICATOR_FILTER_TOO_RESTRICTIVE
    # correctly flag the predicate as unreachable.
    assert report.coverage_category in {
        CoverageCategory.CONJUNCTION_NEVER_TRUE,
        CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE,
    }
    assert report.coverage_category is not CoverageCategory.COVERAGE_OK


def test_reassignment_to_scalar_clears_stale_indicator_binding() -> None:
    """A scalar reassignment after an indicator binding must clear the
    indicator entry from ``name_evaluators``, so downstream predicate
    resolution falls through to the literal value.

    Strategy:
        threshold = sma(close, 5)   # binds indicator
        threshold = 150             # rebinds to scalar
        if close > threshold:       # must evaluate close > 150

    Without the fix, ``_resolve_assign_evaluator(150, ...)`` returns
    None and the existing ``threshold -> sma(close, 5)`` binding stays
    in ``name_evaluators``. ``_build_operand`` consults
    ``name_evaluators`` before numeric literals, so the predicate
    evaluates ``close > sma(close, 5)`` instead of ``close > 150`` —
    on flat ``close=100`` data, that fires roughly half the bars and
    the probe wrongly reports COVERAGE_OK. With the fix the stale
    binding is dropped, ``threshold`` resolves through ``name_periods``
    to 150, ``close > 150`` is unreachable on close=100, and the probe
    flags ``INDICATOR_FILTER_TOO_RESTRICTIVE``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                threshold = sma(close, 5)
                threshold = 150
                if close > threshold:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv(n=60)},
    )
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count == 0


def test_top_level_or_preserves_standalone_symbol_gate() -> None:
    """A top-level OR predicate with a standalone ``bar.symbol == "X"``
    leg must be treated as a firing leg on bars from X. Without this
    fix ``_build_subcond`` drops the gate, the OR collapses to its
    other legs, and a zero-hit price leg falsely flags
    ``INDICATOR_FILTER_TOO_RESTRICTIVE`` even though every AAPL bar
    satisfies the predicate.

    Strategy:
        ``if bar.symbol == "AAPL" or close > 100:``

    Universe: AAPL with close=50 (never > 100). The ``close > 100``
    leg has zero hits but the ``bar.symbol == "AAPL"`` leg fires on
    every AAPL bar — the OR is satisfied so the report must classify
    as ``COVERAGE_OK``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" or close > 100:
                    pass
        """
    )
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    df = pd.DataFrame(
        {
            "open": np.full(n, 50.0),
            "high": np.full(n, 51.0),
            "low": np.full(n, 49.0),
            "close": np.full(n, 50.0),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    # Both legs should surface as coverage rows; the symbol-gate leg
    # fires on every AAPL bar and ``close > 100`` is the zero-hit row.
    assert len(report.subconditions) == 2
    labels = [sc.label for sc in report.subconditions]
    assert any("bar.symbol" in lbl for lbl in labels)
    assert any("close > 100" in lbl for lbl in labels)


def test_later_reassignment_does_not_shadow_earlier_predicate() -> None:
    """A reassignment that appears AFTER a predicate must not shadow
    the binding the predicate sees.

    Strategy:
        ma = sma(close, 5)
        if close > ma:
            pass
        ma = 999          # later reassignment

    Without flow-sensitive bindings, the global pre-pass walked all
    assignments first; with the recent overwrite-on-resolved /
    pop-on-unresolved fix, the trailing ``ma = 999`` cleared the SMA
    binding so ``_build_operand`` resolved ``ma`` through name_periods
    to 999. The predicate evaluated as ``close > 999`` and on flat
    ``close=100`` data the probe wrongly reported
    ``INDICATOR_FILTER_TOO_RESTRICTIVE``. With flow-sensitive
    bindings the predicate sees the SMA binding (the only one in
    scope at its location) and the report classifies as
    ``COVERAGE_OK`` because ``close > sma(close, 5)`` partially fires
    on a swing fixture.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                ma = sma(close, 5)
                if close > ma:
                    pass
                ma = 999
        """
    )
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves = [-0.005] * 8 + [+0.005] * 8 + [-0.005] * 7 + [+0.005] * 7
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count > 0


def test_or_with_unrestricted_leg_treats_warmup_as_universal() -> None:
    """An OR predicate with one symbol-gated leg and one unrestricted
    leg must NOT narrow the warmup denominator to the gate's symbols.

    Strategy:
        ``if bar.symbol == "AAPL" or close > 100:``

    Universe: AAPL with 10 bars, MSFT with 100 bars,
    warmup_bars_required=50.

    The unrestricted ``close > 100`` leg can fire on any symbol, so a
    long enough MSFT history satisfies the warmup check on its own —
    we should NOT short-circuit on ``INSUFFICIENT_BARS``. Without the
    fix the warmup denominator is restricted to AAPL (the only gated
    symbol observed), AAPL's 10 bars < 50, and the probe wrongly
    reports ``INSUFFICIENT_BARS``.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if bar.symbol == "AAPL" or close > 100:
                    pass
        """
    )
    aapl = pd.DataFrame(
        {
            "open": np.full(10, 100.0),
            "high": np.full(10, 101.0),
            "low": np.full(10, 99.0),
            "close": np.full(10, 100.0),
            "volume": np.full(10, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=10, freq="D"),
    )
    msft = pd.DataFrame(
        {
            "open": np.full(100, 200.0),
            "high": np.full(100, 201.0),
            "low": np.full(100, 199.0),
            "close": np.full(100, 200.0),
            "volume": np.full(100, 1_000_000.0),
        },
        index=pd.date_range("2024-01-01", periods=100, freq="D"),
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": aapl, "MSFT": msft},
        warmup_bars_required=50,
    )

    # The OR has an unrestricted leg, so the predicate could fire on
    # any symbol — the warmup check must consider every fetched
    # DataFrame, not just AAPL.
    assert report.coverage_category is not CoverageCategory.INSUFFICIENT_BARS


def test_or_allowlist_propagates_to_and_group() -> None:
    """A nested OR allowlist must restrict the entire AND group.

    Predicate:
        ``(bar.symbol == "AAPL" or bar.symbol == "MSFT") and close > 100``

    Universe: AAPL/MSFT close=50 (never satisfy ``close > 100``),
    GOOG close=200 (would satisfy ``close > 100`` but is not in the
    allowlist).

    Without propagation the group's ``target_symbols`` stays ``None``
    so the sibling ``close > 100`` subcond's hit count includes GOOG
    bars. Both legs then have non-zero hits but their conjunction is
    empty → the probe flags ``CONJUNCTION_NEVER_TRUE``, hiding the
    actionable ``INDICATOR_FILTER_TOO_RESTRICTIVE`` on the gated
    symbols. With propagation the AND group is restricted to
    ``{AAPL, MSFT}``, ``close > 100`` evaluates only against their
    bars (zero hits), and the probe surfaces the indicator filter as
    a blocker.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if (bar.symbol == "AAPL" or bar.symbol == "MSFT") and close > 100:
                    pass
        """
    )

    def _df(close_value: float) -> pd.DataFrame:
        n = 30
        idx = pd.date_range("2024-01-01", periods=n, freq="D")
        return pd.DataFrame(
            {
                "open": np.full(n, close_value),
                "high": np.full(n, close_value + 1.0),
                "low": np.full(n, close_value - 1.0),
                "close": np.full(n, close_value),
                "volume": np.full(n, 1_000_000.0),
            },
            index=idx,
        )

    report = run_indicator_probe(
        strategy_code=code,
        market_data={
            "AAPL": _df(50.0),
            "MSFT": _df(50.0),
            "GOOG": _df(200.0),
        },
    )

    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    # The actionable blocker is the ``close > 100`` zero-hit on the
    # allowlisted symbols, not a conjunction issue.
    assert report.likely_blockers
    blocker_reasons = {b.reason for b in report.likely_blockers}
    assert "indicator_filter_zero_hits" in blocker_reasons
    assert "conjunction_never_true" not in blocker_reasons


def test_helper_class_does_not_shadow_strategy_period_constant() -> None:
    """A sibling helper class with the same attribute name must not
    pre-empt the strategy class's bare-name period binding.

    Strategy code::

        class Helper:
            PERIOD = 2

        class Strategy:
            PERIOD = 200

            def on_bar(self, ctx, bar):
                if close > sma(close, self.PERIOD):
                    pass

    On a 30-bar fixture, ``sma(close, 200)`` has no warmup-complete
    bars so ``close > sma(...)`` has zero hits and the probe
    classifies ``INDICATOR_FILTER_TOO_RESTRICTIVE``. With the bug the
    global ``setdefault`` walk picks up ``Helper.PERIOD = 2`` first
    (BFS source order) and ``self.PERIOD`` resolves to 2; ``sma(close,
    2)`` is defined after 2 bars and fires roughly half the bars,
    flipping the report to ``COVERAGE_OK``.

    With the fix, ``_find_strategy_class`` identifies the class
    containing ``on_bar`` and ``_collect_name_periods`` skips
    ``Helper``, so ``self.PERIOD`` correctly resolves to 200.
    """
    code = textwrap.dedent(
        """
        class Helper:
            PERIOD = 2

        class Strategy:
            PERIOD = 200

            def on_bar(self, ctx, bar):
                if close > sma(close, self.PERIOD):
                    pass
        """
    )
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves = [-0.005] * 8 + [+0.005] * 8 + [-0.005] * 7 + [+0.005] * 7
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    # PERIOD=200 on 30 bars → all NaN → zero hits.
    # PERIOD=2 (the helper's value, used pre-fix) → many hits.
    assert report.coverage_category is CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert len(report.subconditions) == 1
    assert report.subconditions[0].hit_count == 0


def test_helper_class_period_does_not_apply_when_strategy_constant_missing() -> None:
    """Sanity: when the strategy class has no constant of its own and
    a helper class has one, the strategy still cannot pull from the
    helper. Module-level constants remain accessible.

    Strategy code::

        WINDOW = 5

        class Helper:
            PERIOD = 999

        class Strategy:
            def on_bar(self, ctx, bar):
                if close > sma(close, WINDOW):
                    pass

    Module-level ``WINDOW = 5`` is still visible (it's outside any
    class), but Helper's ``PERIOD = 999`` is not used because the
    strategy never references ``self.PERIOD`` anyway.
    """
    code = textwrap.dedent(
        """
        WINDOW = 5

        class Helper:
            PERIOD = 999

        class Strategy:
            def on_bar(self, ctx, bar):
                if close > sma(close, WINDOW):
                    pass
        """
    )
    n = 30
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves = [-0.005] * 8 + [+0.005] * 8 + [-0.005] * 7 + [+0.005] * 7
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    df = pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )
    report = run_indicator_probe(strategy_code=code, market_data={"AAPL": df})

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert report.subconditions[0].hit_count > 0


def test_or_with_unknown_leg_suppresses_false_restrictive_blocker() -> None:
    """When an OR predicate has an unrecognised leg (e.g. a custom
    method call we can't model), the probe must NOT flag
    ``INDICATOR_FILTER_TOO_RESTRICTIVE`` based only on the recognised
    legs being zero — the unknown alternative may make the entry
    reachable.

    Strategy:
        ``if self.custom_ok(bar) or close < -50:``

    Universe: flat ``close=100`` (never satisfies ``close < -50``).
    Without the fix the un-modelled ``self.custom_ok(bar)`` leg is
    silently dropped, the surviving ``close < -50`` leg has zero hits,
    and the aggregator emits an ``or_group_never_fires`` blocker —
    classifying ``INDICATOR_FILTER_TOO_RESTRICTIVE`` even though the
    custom call may make the predicate fire. With the fix the
    ``has_unknown_or_leg`` flag suppresses the blocker.
    """
    code = textwrap.dedent(
        """
        class S:
            def on_bar(self, ctx, bar):
                if self.custom_ok(bar) or close < -50:
                    pass
        """
    )
    report = run_indicator_probe(
        strategy_code=code,
        market_data={"AAPL": _flat_ohlcv()},
    )

    assert report.coverage_category is not CoverageCategory.INDICATOR_FILTER_TOO_RESTRICTIVE
    assert all(b.reason != "or_group_never_fires" for b in report.likely_blockers)


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
