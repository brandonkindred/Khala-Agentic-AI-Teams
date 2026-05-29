"""Coverage for ``strategy_lab.factors.compiler._lookback``.

The lookback helper walks the genome tree and returns the maximum warm-up
window required by any node. Each branch corresponds to a different node
type — tests instantiate each one in isolation.
"""

from __future__ import annotations

import pytest

from investment_team.strategy_lab.factors.compiler import _lookback
from investment_team.strategy_lab.factors.models import (
    ADX,
    ATR,
    EMA,
    RSI,
    SMA,
    VWAP,
    ATRBreakout,
    BollingerZ,
    BoolAnd,
    BoolNot,
    BoolOr,
    CompareGT,
    CompareLT,
    Const,
    CrossOver,
    CrossUnder,
    FundingRateDeviation,
    IfRegime,
    MACDSignal,
    MomentumK,
    Price,
    Skew,
    StochasticK,
    TermStructureSlope,
    VolRegimeState,
    WeightedSum,
    ZScoreResidualOLS,
)

# ---------------------------------------------------------------------------
# Single-node lookbacks
# ---------------------------------------------------------------------------


def test_price_and_const_lookback_is_one() -> None:
    assert _lookback(Price(field="close")) == 1
    assert _lookback(Const(value=1.5)) == 1


def test_sma_ema_lookback_equals_period() -> None:
    assert _lookback(SMA(period=20)) == 20
    assert _lookback(EMA(period=12)) == 12


def test_rsi_lookback_is_period_plus_one() -> None:
    assert _lookback(RSI(period=14)) == 15


def test_macd_signal_lookback_is_slow_plus_signal_minus_one() -> None:
    # Signal-EMA fills at ``len(macd_line) >= signal``, i.e. at
    # ``len(bars) == slow + signal - 1``. Matches the registry's
    # ``_macd_value`` gate and the synthesis compiler's ``_lookback_for``
    # for ``output='signal'``. Prior version was off-by-one (returned
    # ``slow + signal``), so factors-compiled MACDSignal genomes fired
    # signals one bar later than equivalent synthesis-spec strategies.
    assert _lookback(MACDSignal(fast=12, slow=26, signal=9)) == 34


def test_atr_lookback_is_period_plus_one() -> None:
    assert _lookback(ATR(period=14)) == 15


def test_adx_lookback_is_2x_period_plus_one() -> None:
    assert _lookback(ADX(period=14)) == 29


def test_momentum_k_lookback_is_k_plus_one() -> None:
    assert _lookback(MomentumK(k=5)) == 6


def test_zscore_residual_ols_lookback_is_window() -> None:
    assert _lookback(ZScoreResidualOLS(window=30, vs_symbol="SPY")) == 30


def test_skew_lookback_is_window_plus_one() -> None:
    assert _lookback(Skew(window=20)) == 21


def test_vol_regime_lookback_is_lookback_plus_one() -> None:
    assert _lookback(VolRegimeState(lookback=20, threshold=1.2)) == 21


def test_term_structure_and_funding_rate_lookback_is_one() -> None:
    assert _lookback(TermStructureSlope(front_symbol="VX1", back_symbol="VX2", window=5)) == 1
    assert _lookback(FundingRateDeviation(symbol="BTC", lookback=10)) == 1


def test_bollinger_z_stochastic_vwap_lookback_equals_period() -> None:
    assert _lookback(BollingerZ(period=20)) == 20
    assert _lookback(StochasticK(period=14)) == 14
    assert _lookback(VWAP(period=10)) == 10


# ---------------------------------------------------------------------------
# Combinators
# ---------------------------------------------------------------------------


def test_weighted_sum_lookback_takes_max_child() -> None:
    node = WeightedSum(
        children=[SMA(period=10), SMA(period=50)],
        weights=[0.5, 0.5],
    )
    assert _lookback(node) == 50


def test_if_regime_lookback_takes_max_of_branches() -> None:
    """gate must be a BoolNode; if_true / if_false are NumNodes."""
    node = IfRegime(
        gate=CompareGT(left=SMA(period=10), right=Const(value=1.0)),
        if_true=SMA(period=5),
        if_false=SMA(period=100),
    )
    # max(_lookback(gate), _lookback(if_true), _lookback(if_false)) = max(10, 5, 100) = 100
    assert _lookback(node) == 100


def test_compare_lookback_takes_max_of_sides() -> None:
    assert _lookback(CompareGT(left=SMA(period=20), right=Const(value=1))) == 20
    assert _lookback(CompareLT(left=Const(value=1), right=EMA(period=15))) == 15


def test_crossover_lookback_takes_max_plus_one() -> None:
    # CrossOver/CrossUnder require one extra bar to compare consecutive values.
    assert _lookback(CrossOver(fast=SMA(period=5), slow=SMA(period=20))) == 21
    assert _lookback(CrossUnder(fast=SMA(period=5), slow=SMA(period=20))) == 21


def test_atr_breakout_lookback_uses_max() -> None:
    node = ATRBreakout(k=10, atr_period=14)
    # max(k + 1, atr_period + 1) = max(11, 15) = 15
    assert _lookback(node) == 15


def test_bool_and_or_lookback_takes_max_child() -> None:
    children = [
        CompareGT(left=SMA(period=10), right=Const(value=1)),
        CompareGT(left=EMA(period=50), right=Const(value=1)),
    ]
    assert _lookback(BoolAnd(children=children)) == 50
    assert _lookback(BoolOr(children=children)) == 50


def test_bool_not_lookback_equals_child() -> None:
    node = BoolNot(child=CompareGT(left=SMA(period=30), right=Const(value=1)))
    assert _lookback(node) == 30


def test_lookback_raises_on_unknown_node() -> None:
    """Defensive: the dispatcher TypeErrors on unrecognised types."""
    with pytest.raises(TypeError):
        _lookback("not a node")  # type: ignore[arg-type]
