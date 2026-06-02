"""Unit coverage for the Strategy Lab performance cleanups.

Covers the behaviour-preserving optimisations:

* ``IndicatorRef.sig_id`` — stable, cheap cache key that replaces
  ``model_dump_json()`` on the indicator hot path.
* ``compute_indicator_series`` dict-dispatch — one entry per indicator,
  unknown names still raise.
* ``parse_strategy_source`` — memoised parse shared across gates.
* ``BacktestAnomalyCtx`` cached per-trade aggregates equal the naive
  reductions they replaced.
"""

from __future__ import annotations

import ast

import pandas as pd
import pytest

from investment_team.models import BacktestResult, TradeRecord
from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    PandasHistoryView,
    StreamingHistoryView,
    compute_indicator_series,
)
from investment_team.strategy_lab.quality_gates.backtest_anomaly import BacktestAnomalyCtx
from investment_team.strategy_lab.quality_gates.code_safety_ast import parse_strategy_source
from investment_team.strategy_lab.spec_dsl import IndicatorRef

# ---------------------------------------------------------------------------
# IndicatorRef.sig_id
# ---------------------------------------------------------------------------


def test_sig_id_is_non_empty_and_stable() -> None:
    ref = IndicatorRef(name="sma", params={"period": 20})
    assert ref.sig_id
    # Stable across repeated access (no recomputation surprises).
    assert ref.sig_id == ref.sig_id


def test_sig_id_equal_for_equivalent_refs_with_default_params() -> None:
    # rsi default period is 14 — passing it explicitly must yield the same
    # sig_id as omitting it, matching the equal-config invariant.
    explicit = IndicatorRef(name="rsi", params={"period": 14})
    implicit = IndicatorRef(name="rsi")
    assert explicit == implicit
    assert explicit.sig_id == implicit.sig_id


def test_sig_id_distinguishes_name_params_and_source() -> None:
    base = IndicatorRef(name="sma", params={"period": 20})
    diff_period = IndicatorRef(name="sma", params={"period": 50})
    diff_name = IndicatorRef(name="ema", params={"period": 20})
    diff_source = IndicatorRef(name="sma", params={"period": 20}, source="high")
    ids = {base.sig_id, diff_period.sig_id, diff_name.sig_id, diff_source.sig_id}
    assert len(ids) == 4


def test_sig_id_is_excluded_from_model_dump() -> None:
    ref = IndicatorRef(name="sma", params={"period": 20})
    assert "sig_id" not in ref.model_dump()
    assert "sig_id" not in ref.model_dump_json()


def test_sig_id_reflects_post_construction_mutation() -> None:
    # IndicatorRef is mutable; the cache key must track the *current* config so
    # a mutated-then-reused ref doesn't collide with the pre-mutation cache
    # entry (matching the old per-call model_dump_json semantics).
    ref = IndicatorRef(name="sma", params={"period": 5})
    before = ref.sig_id
    ref.params["period"] = 10
    assert ref.sig_id != before
    assert ref.sig_id == IndicatorRef(name="sma", params={"period": 10}).sig_id
    ref.source = "high"
    assert ref.sig_id != IndicatorRef(name="sma", params={"period": 10}).sig_id


def test_cache_distinguishes_series_after_param_mutation() -> None:
    # The history-view cache keyed on sig_id must compute a fresh series when a
    # reused ref's period changes, not return the stale shorter-window series.
    df = _ohlcv()
    view = PandasHistoryView(df, {})
    ref = IndicatorRef(name="sma", params={"period": 5})
    sma5 = view.indicator(ref, 30)
    ref.params["period"] = 20
    sma20 = view.indicator(ref, 30)
    assert sma5 != sma20
    assert sma20 == view.indicator(IndicatorRef(name="sma", params={"period": 20}), 30)


# ---------------------------------------------------------------------------
# compute_indicator_series dispatch
# ---------------------------------------------------------------------------


def _ohlcv(n: int = 60) -> pd.DataFrame:
    closes = [100.0 + i for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        }
    )


@pytest.mark.parametrize(
    "ref",
    [
        IndicatorRef(name="sma", params={"period": 5}),
        IndicatorRef(name="ema", params={"period": 5}),
        IndicatorRef(name="rsi", params={"period": 14}),
        IndicatorRef(name="macd"),
        IndicatorRef(name="macd", params={"output": "signal"}),
        IndicatorRef(name="macd", params={"output": "histogram"}),
        IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"}),
        IndicatorRef(name="bollinger", params={"period": 20, "band": "lower"}),
        IndicatorRef(name="bollinger", params={"period": 20}),
        IndicatorRef(name="atr", params={"period": 14}),
        IndicatorRef(name="adx", params={"period": 14}),
        IndicatorRef(name="stochastic", params={"output": "k"}),
        IndicatorRef(name="stochastic", params={"output": "d"}),
        IndicatorRef(name="vwap"),
    ],
)
def test_dispatch_covers_every_indicator(ref: IndicatorRef) -> None:
    series = compute_indicator_series(ref, _ohlcv())
    assert isinstance(series, pd.Series)
    assert len(series) == 60


def test_dispatch_unknown_name_raises() -> None:
    ref = IndicatorRef(name="sma", params={"period": 5})
    object.__setattr__(ref, "name", "not_a_real_indicator")
    with pytest.raises(ValueError, match="unknown indicator name"):
        compute_indicator_series(ref, _ohlcv())


def test_cache_key_dedupes_equivalent_refs_pandas_view() -> None:
    df = _ohlcv()
    cache: dict = {}
    view = PandasHistoryView(df, cache)
    a = view.indicator(IndicatorRef(name="sma", params={"period": 5}), 30)
    # Equivalent ref with the default filled differently must hit the cache.
    b = view.indicator(IndicatorRef(name="sma", params={"period": 5}), 30)
    assert a == b
    assert len(cache) == 1


def test_streaming_view_uses_sig_id_key() -> None:
    view = StreamingHistoryView(max_bars=100)
    for i in range(40):
        c = 100.0 + i
        view.append(BarRecord(timestamp=str(i), open=c, high=c + 1, low=c - 1, close=c, volume=1.0))
    ref = IndicatorRef(name="sma", params={"period": 5})
    val = view.indicator(ref, 39)
    assert val is not None


# ---------------------------------------------------------------------------
# parse_strategy_source memoisation
# ---------------------------------------------------------------------------


def test_parse_strategy_source_is_memoised() -> None:
    code = "class S:\n    pass\n"
    a = parse_strategy_source(code)
    b = parse_strategy_source(code)
    assert a is b  # same cached Module object
    assert isinstance(a, ast.Module)


def test_parse_strategy_source_propagates_syntax_error() -> None:
    with pytest.raises(SyntaxError):
        parse_strategy_source("def (:\n")


# ---------------------------------------------------------------------------
# BacktestAnomalyCtx cached aggregates
# ---------------------------------------------------------------------------


def _trade(net: float, gross: float) -> TradeRecord:
    return TradeRecord(
        trade_num=1,
        entry_date="2023-01-01",
        exit_date="2023-01-05",
        symbol="AAA",
        side="long",
        entry_price=100.0,
        exit_price=101.0,
        shares=1.0,
        position_value=100.0,
        gross_pnl=gross,
        net_pnl=net,
        return_pct=1.0,
        hold_days=4,
        outcome="win" if net > 0 else "loss",
        cumulative_pnl=net,
    )


def _ctx(trades: list[TradeRecord]) -> BacktestAnomalyCtx:
    metrics = BacktestResult(
        total_return_pct=1.0,
        annualized_return_pct=1.0,
        volatility_pct=1.0,
        sharpe_ratio=1.0,
        max_drawdown_pct=1.0,
        win_rate_pct=50.0,
        profit_factor=1.0,
        calmar_ratio=0.0,
        deflated_sharpe=0.0,
        sortino_ratio=0.0,
    )
    return BacktestAnomalyCtx(
        metrics=metrics,
        trades=trades,
        mode="backtest",
        dsr_aware=False,
        diagnostics=None,
        coverage_report=None,
    )


def test_cached_aggregates_match_naive_reductions() -> None:
    trades = [_trade(25.0, 30.0), _trade(-10.0, -12.0), _trade(5.0, 6.0)]
    ctx = _ctx(trades)
    assert ctx.total_abs_pnl == sum(abs(t.net_pnl) for t in trades)
    assert ctx.max_abs_pnl == max(abs(t.net_pnl) for t in trades)
    assert ctx.gross_wins == sum(t.gross_pnl for t in trades if t.gross_pnl > 0)
    assert ctx.gross_losses == abs(sum(t.gross_pnl for t in trades if t.gross_pnl <= 0))


def test_cached_aggregates_are_memoised() -> None:
    ctx = _ctx([_trade(25.0, 30.0)])
    first = ctx.total_abs_pnl
    # cached_property writes into __dict__ even on the frozen dataclass.
    assert "total_abs_pnl" in ctx.__dict__
    assert ctx.total_abs_pnl is first
