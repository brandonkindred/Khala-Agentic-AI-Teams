"""Tests for the static/code-aware coverage probe (#447)."""

from __future__ import annotations

import textwrap

from investment_team.models import (
    CoverageCategory,
    StrategySpec,
)
from investment_team.strategy_lab.coverage_probe import run_static_probe
from investment_team.strategy_lab.spec_dsl import (
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
)


def _spec(strategy_code: str | None, *, max_position_pct: float = 6.0) -> StrategySpec:
    return StrategySpec(
        strategy_id="strat-test",
        authored_by="test",
        asset_class="stocks",
        hypothesis="hyp",
        signal_definition="sig",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op="<", rhs=30),
            )
        ],
        exit_rules=[
            SignalExitRule(
                when=Predicate(lhs=IndicatorRef(name="rsi", params={"period": 14}), op=">", rhs=70)
            )
        ],
        risk_limits={"max_position_pct": max_position_pct},
        speculative=False,
        strategy_code=strategy_code,
    )


def test_clean_factor_compiled_code_returns_coverage_ok() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy, OrderSide, OrderType

        class S(Strategy):
            MIN_HISTORY = 20

            def on_bar(self, ctx, bar):
                bars = ctx.history(bar.symbol, self.MIN_HISTORY + 4)
                if len(bars) < self.MIN_HISTORY:
                    return
                pos = ctx.position(bar.symbol)
                if pos is None:
                    ctx.submit_order(
                        symbol=bar.symbol,
                        side=OrderSide.LONG,
                        qty=10,
                        order_type=OrderType.MARKET,
                        reason="entry",
                    )
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL", "MSFT"], available_bars=250)

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert report.likely_blockers == []
    assert report.warmup_bars_required == 20
    assert report.symbols_checked == 2
    assert report.bars_checked == 250
    assert report.entry_orders_emitted == 0


def test_warmup_exceeds_history_takes_priority() -> None:
    code = "WINDOW = 500\n"
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=200)

    assert report.coverage_category is CoverageCategory.WARMUP_EXCEEDS_HISTORY
    assert report.warmup_bars_required == 500
    assert any(b.reason == "warmup_exceeds_history" for b in report.likely_blockers)
    assert "500" in report.summary and "200" in report.summary


def test_missing_target_symbol_flagged() -> None:
    code = textwrap.dedent(
        """
        def on_bar(self, ctx, bar):
            ctx.submit_order(symbol="TSLA", side="LONG", qty=10)
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL", "MSFT"], available_bars=250)

    assert report.coverage_category is CoverageCategory.TARGET_SYMBOL_MISSING
    assert any(
        b.reason == "target_symbol_missing" and "TSLA" in b.evidence for b in report.likely_blockers
    )


def test_warmup_takes_priority_over_missing_symbol() -> None:
    code = textwrap.dedent(
        """
        WINDOW = 800

        def on_bar(self, ctx, bar):
            ctx.submit_order(symbol="TSLA", side="LONG", qty=10)
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=200)

    assert report.coverage_category is CoverageCategory.WARMUP_EXCEEDS_HISTORY
    reasons = {b.reason for b in report.likely_blockers}
    assert "warmup_exceeds_history" in reasons
    assert "target_symbol_missing" in reasons


def test_position_pct_over_limit_does_not_change_category() -> None:
    code = "POSITION_PCT = 15.0\n"
    report = run_static_probe(
        _spec(code, max_position_pct=6.0),
        fetched_universe=["AAPL"],
        available_bars=250,
    )

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert any(b.reason == "position_pct_exceeds_risk_limit" for b in report.likely_blockers)


def test_fractional_position_pct_is_normalized_against_percent_limit() -> None:
    # The ideation prompt documents ``qty = ctx.equity * pct / bar.close``,
    # so ``POSITION_PCT = 0.10`` is a 10% fraction. RiskLimits.max_position_pct
    # is 6.0 (i.e. 6%), so this must flag an oversize position.
    code = "POSITION_PCT = 0.10\n"
    report = run_static_probe(
        _spec(code, max_position_pct=6.0),
        fetched_universe=["AAPL"],
        available_bars=250,
    )

    over_limit = [
        b for b in report.likely_blockers if b.reason == "position_pct_exceeds_risk_limit"
    ]
    assert len(over_limit) == 1
    # Evidence should make the conversion auditable.
    assert "10" in over_limit[0].evidence and "0.1" in over_limit[0].evidence


def test_fractional_position_pct_within_limit_not_flagged() -> None:
    code = "POSITION_PCT = 0.05\n"  # 5%, under the 6% limit
    report = run_static_probe(
        _spec(code, max_position_pct=6.0),
        fetched_universe=["AAPL"],
        available_bars=250,
    )

    assert all(b.reason != "position_pct_exceeds_risk_limit" for b in report.likely_blockers)


def test_full_equity_fraction_is_flagged() -> None:
    # ``pct = 1.0`` means 100% of equity in the documented fraction
    # convention — far over any reasonable max_position_pct.
    code = "POSITION_PCT = 1.0\n"
    report = run_static_probe(
        _spec(code, max_position_pct=6.0),
        fetched_universe=["AAPL"],
        available_bars=250,
    )

    assert any(b.reason == "position_pct_exceeds_risk_limit" for b in report.likely_blockers)


def test_pct_kwarg_fractional_value_is_normalized() -> None:
    code = textwrap.dedent(
        """
        def on_bar(self, ctx, bar):
            ctx.submit_order(symbol=bar.symbol, side="LONG", qty=10, pct=0.20)
        """
    )
    report = run_static_probe(
        _spec(code, max_position_pct=6.0),
        fetched_universe=["AAPL"],
        available_bars=250,
    )

    assert any(b.reason == "position_pct_exceeds_risk_limit" for b in report.likely_blockers)


def test_malformed_code_returns_unknown_low_coverage() -> None:
    report = run_static_probe(
        _spec("def on_bar(:::"),
        fetched_universe=["AAPL"],
        available_bars=250,
    )

    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert "did not parse" in report.summary
    assert report.likely_blockers == []


def test_none_strategy_code_returns_unknown_low_coverage() -> None:
    report = run_static_probe(_spec(None), fetched_universe=["AAPL"], available_bars=250)

    assert report.coverage_category is CoverageCategory.UNKNOWN_LOW_COVERAGE
    assert "no strategy_code" in report.summary


def test_dynamic_bar_symbol_only_does_not_flag_symbol() -> None:
    code = textwrap.dedent(
        """
        def on_bar(self, ctx, bar):
            ctx.submit_order(symbol=bar.symbol, side="LONG", qty=10)
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=250)

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert all(b.reason != "target_symbol_missing" for b in report.likely_blockers)


def test_class_attribute_period_is_extracted() -> None:
    code = textwrap.dedent(
        """
        class S:
            WINDOW = 80
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=250)

    assert report.warmup_bars_required == 80
    assert report.coverage_category is CoverageCategory.COVERAGE_OK


def test_inline_history_call_arg_is_extracted() -> None:
    code = textwrap.dedent(
        """
        def on_bar(self, ctx, bar):
            bars = ctx.history(bar.symbol, 100)
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=250)

    assert report.warmup_bars_required == 100


def test_period_alias_names_are_picked_up() -> None:
    code = textwrap.dedent(
        """
        RSI_PERIOD = 14
        SMA_LOOKBACK = 200
        LOOKBACK_LONG = 50
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=300)

    assert report.warmup_bars_required == 200


def test_duplicate_missing_symbols_yield_one_blocker_per_unique_symbol() -> None:
    code = textwrap.dedent(
        """
        def on_bar(self, ctx, bar):
            ctx.submit_order(symbol="TSLA", side="LONG", qty=1)
            ctx.submit_order(symbol="TSLA", side="SHORT", qty=1)
            ctx.submit_order(symbol="NVDA", side="LONG", qty=1)
        """
    )
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=250)

    missing_blockers = [b for b in report.likely_blockers if b.reason == "target_symbol_missing"]
    assert len(missing_blockers) == 2
    evidence_text = " ".join(b.evidence for b in missing_blockers)
    assert "TSLA" in evidence_text and "NVDA" in evidence_text


def test_zero_or_negative_available_bars_does_not_trigger_warmup_blocker() -> None:
    code = "WINDOW = 50\n"
    report = run_static_probe(_spec(code), fetched_universe=["AAPL"], available_bars=0)

    assert report.coverage_category is CoverageCategory.COVERAGE_OK
    assert report.warmup_bars_required == 50
    assert all(b.reason != "warmup_exceeds_history" for b in report.likely_blockers)
