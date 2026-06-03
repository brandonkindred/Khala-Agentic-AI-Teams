"""Tests for the ``ctx.indicator(...)`` accessor and its conformance recognition.

Covers three surfaces that route indicator reads through one prescriptive,
engine-backed accessor:

* ``strategy_lab.executor.strategy_indicators.indicator_value`` — the shared
  scalar accessor, asserted byte-equal to the engine's ``StreamingHistoryView``
  for every DSL indicator (the single-source-of-truth guarantee).
* ``StrategyContext.indicator`` (subprocess runtime) and
  ``_ShadowContext.indicator`` (predicate-conformance shadow) — both delegate to
  the same accessor over the bars they already retain.
* ``CodeConformanceGate`` check #1 — now recognises ``ctx.indicator('<name>')``
  in addition to the legacy named-call form, without breaking the latter.
"""

from __future__ import annotations

import random
import textwrap

import pytest

from investment_team.models import StrategySpec
from investment_team.strategy_lab.executor.predicate_evaluator import (
    BarRecord,
    StreamingHistoryView,
)
from investment_team.strategy_lab.executor.strategy_indicators import indicator_value
from investment_team.strategy_lab.quality_gates.code_conformance import CodeConformanceGate
from investment_team.strategy_lab.quality_gates.predicate_conformance import (
    _ShadowBar,
    _ShadowContext,
)
from investment_team.strategy_lab.spec_dsl import (
    DEFAULT_SIZING_PAYLOAD,
    EntryRule,
    IndicatorRef,
    Predicate,
    SignalExitRule,
    StopLossRule,
)
from investment_team.trading_service.strategy.contract import Bar, StrategyContext

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bars(n: int = 60, *, symbol: str = "QQQ") -> list[Bar]:
    """Deterministic OHLCV bars with genuine intrabar range (so ATR/ADX/Stoch
    are non-degenerate)."""
    rng = random.Random(7)
    bars: list[Bar] = []
    px = 100.0
    for i in range(n):
        px *= 1 + rng.uniform(-0.02, 0.025)
        high = px * (1 + rng.uniform(0.0, 0.012))
        low = px * (1 - rng.uniform(0.0, 0.012))
        opn = low + (high - low) * rng.random()
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=f"2026-02-{(i % 28) + 1:02d}T00:00:00Z",
                timeframe="1d",
                open=opn,
                high=high,
                low=low,
                close=px,
                volume=1000.0 + i,
            )
        )
    return bars


def _engine_view(bars: list[Bar]) -> StreamingHistoryView:
    view = StreamingHistoryView()
    for b in bars:
        view.append(
            BarRecord(
                timestamp=b.timestamp,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
        )
    return view


def _engine_latest(view: StreamingHistoryView, ref: IndicatorRef):
    return view.indicator(ref, view.length() - 1)


# (accessor kwargs, IndicatorRef) pairs spanning every DSL indicator + selector.
_PARITY_CASES = [
    ({"name": "sma", "period": 20}, IndicatorRef(name="sma", params={"period": 20})),
    ({"name": "ema", "period": 12}, IndicatorRef(name="ema", params={"period": 12})),
    ({"name": "rsi", "period": 14}, IndicatorRef(name="rsi", params={"period": 14})),
    (
        {"name": "macd", "output": "histogram"},
        IndicatorRef(name="macd", params={"output": "histogram"}),
    ),
    (
        {"name": "macd", "output": "signal"},
        IndicatorRef(name="macd", params={"output": "signal"}),
    ),
    (
        {"name": "bollinger", "period": 20, "band": "upper"},
        IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"}),
    ),
    (
        {"name": "bollinger", "band": "lower"},
        IndicatorRef(name="bollinger", params={"band": "lower"}),
    ),
    ({"name": "atr", "period": 14}, IndicatorRef(name="atr", params={"period": 14})),
    ({"name": "adx", "period": 14}, IndicatorRef(name="adx", params={"period": 14})),
    (
        {"name": "stochastic", "output": "d"},
        IndicatorRef(name="stochastic", params={"output": "d"}),
    ),
    ({"name": "vwap"}, IndicatorRef(name="vwap", params={})),
]


# ---------------------------------------------------------------------------
# indicator_value — parity with the engine (single source of truth)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs,ref", _PARITY_CASES)
def test_indicator_value_matches_engine_view(kwargs, ref) -> None:
    bars = _make_bars()
    view = _engine_view(bars)
    got = indicator_value(history=bars, **kwargs)
    exp = _engine_latest(view, ref)
    assert exp is not None, "fixture should be past warm-up for every indicator"
    assert got == pytest.approx(exp, rel=1e-9, abs=1e-9)


def test_indicator_value_source_override_matches_engine() -> None:
    bars = _make_bars()
    view = _engine_view(bars)
    for source in ("hl2", "ohlc4", "high", "low"):
        got = indicator_value("sma", bars, period=10, source=source)
        exp = _engine_latest(view, IndicatorRef(name="sma", params={"period": 10}, source=source))
        assert got == pytest.approx(exp, rel=1e-9, abs=1e-9), source


def test_indicator_value_warmup_and_empty_return_none() -> None:
    assert indicator_value("sma", [], period=20) is None
    assert indicator_value("sma", _make_bars(5), period=20) is None  # < period
    assert indicator_value("rsi", _make_bars(3)) is None


def test_indicator_value_accepts_plain_number_sequence() -> None:
    closes = [float(x) for x in range(1, 41)]
    assert indicator_value("sma", closes, period=5) == pytest.approx(38.0)


# ---------------------------------------------------------------------------
# indicator_value — contract violations raise (DbC)
# ---------------------------------------------------------------------------


def test_indicator_value_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown indicator"):
        indicator_value("supertrend", _make_bars(), period=10)


def test_indicator_value_missing_required_period_raises() -> None:
    with pytest.raises(ValueError, match="requires a 'period'"):
        indicator_value("sma", _make_bars())
    with pytest.raises(ValueError, match="requires a 'period'"):
        indicator_value("ema", _make_bars())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "macd", "output": "nope"},
        {"name": "bollinger", "band": "sideways"},
        {"name": "stochastic", "output": "z"},
    ],
)
def test_indicator_value_bad_selector_raises(kwargs) -> None:
    with pytest.raises(ValueError, match="invalid selector"):
        indicator_value(history=_make_bars(), **kwargs)


def test_indicator_value_bad_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown indicator source"):
        indicator_value("sma", _make_bars(), period=10, source="median")


# ---------------------------------------------------------------------------
# StrategyContext.indicator
# ---------------------------------------------------------------------------


def test_strategy_context_indicator_matches_engine_and_defaults_to_current_symbol() -> None:
    bars = _make_bars()
    view = _engine_view(bars)
    ctx = StrategyContext(emit=lambda _d: None)
    for b in bars:
        ctx._ingest_bar(b)
    got = ctx.indicator("sma", period=20)  # no symbol → current bar's symbol
    exp = _engine_latest(view, IndicatorRef(name="sma", params={"period": 20}))
    assert got == pytest.approx(exp, rel=1e-9)


def test_strategy_context_indicator_multi_symbol_isolation() -> None:
    a = _make_bars(symbol="AAA")
    b = _make_bars(symbol="BBB")
    ctx = StrategyContext(emit=lambda _d: None)
    # Interleave so the "current symbol" is BBB at the end.
    for ba, bb in zip(a, b):
        ctx._ingest_bar(ba)
        ctx._ingest_bar(bb)
    by_default = ctx.indicator("sma", period=10)
    explicit_bbb = ctx.indicator("sma", period=10, symbol="BBB")
    explicit_aaa = ctx.indicator("sma", period=10, symbol="AAA")
    assert by_default == pytest.approx(explicit_bbb)
    assert explicit_aaa == pytest.approx(
        _engine_latest(_engine_view(a), IndicatorRef(name="sma", params={"period": 10}))
    )


def test_strategy_context_indicator_no_bar_yet_raises() -> None:
    ctx = StrategyContext(emit=lambda _d: None)
    with pytest.raises(ValueError, match="no bar has been dispatched"):
        ctx.indicator("sma", period=20)


def test_strategy_context_indicator_unknown_symbol_returns_none() -> None:
    ctx = StrategyContext(emit=lambda _d: None)
    for b in _make_bars(10):
        ctx._ingest_bar(b)
    assert ctx.indicator("sma", period=5, symbol="ZZZ") is None


# ---------------------------------------------------------------------------
# _ShadowContext.indicator (predicate-conformance shadow)
# ---------------------------------------------------------------------------


def _shadow_bars(n: int = 60) -> list[_ShadowBar]:
    return [
        _ShadowBar(
            symbol="QQQ",
            timestamp=f"2026-02-{(i % 28) + 1:02d}T00:00:00Z",
            timeframe="1d",
            open=100.0 + i,
            high=101.5 + i,
            low=99.0 + i,
            close=100.0 + i,
            volume=1000.0 + i,
        )
        for i in range(n)
    ]


def test_shadow_context_indicator_matches_real_context() -> None:
    sbars = _shadow_bars()
    shadow = _ShadowContext()
    for i, b in enumerate(sbars):
        shadow._ingest_bar(b, i)
    # Identical-valued real bars for cross-check.
    real = StrategyContext(emit=lambda _d: None)
    for b in sbars:
        real._ingest_bar(
            Bar(
                symbol=b.symbol,
                timestamp=b.timestamp,
                timeframe=b.timeframe,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
            )
        )
    assert shadow.indicator("ema", period=10) == pytest.approx(real.indicator("ema", period=10))


def test_shadow_context_indicator_warmup_and_no_bar() -> None:
    shadow = _ShadowContext()
    assert shadow.indicator("sma", period=20) is None  # no bar ingested yet
    shadow._ingest_bar(_shadow_bars(1)[0], 0)
    assert shadow.indicator("sma", period=20) is None  # warm-up


# ---------------------------------------------------------------------------
# CodeConformanceGate check #1 — ctx.indicator recognition (additive)
# ---------------------------------------------------------------------------


def _ema_rsi_spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="t-ctx-1",
        authored_by="test",
        asset_class="stocks",
        hypothesis="EMA trend with RSI exit.",
        signal_definition="ema(20) vs close; rsi(14) > 70",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="ema", params={"period": 20}),
                    op=">",
                    rhs="bar.close",
                ),
            )
        ],
        exit_rules=[
            SignalExitRule(when=Predicate(lhs=IndicatorRef(name="rsi"), op=">", rhs=70.0)),
            StopLossRule(pct=0.05),
        ],
        sizing=DEFAULT_SIZING_PAYLOAD,
        target_symbols=["QQQ"],
    )


def _critical_details(results) -> list[str]:
    return [r.details for r in results if r.severity == "critical" and not r.passed]


_CTX_INDICATOR_CODE = textwrap.dedent(
    """
    from contract import Strategy

    class S(Strategy):
        UNIVERSE = frozenset({"QQQ"})

        def on_bar(self, ctx, bar):
            if bar.symbol not in self.UNIVERSE:
                return
            trend = ctx.indicator('ema', period=20)
            r = ctx.indicator('rsi', period=14)
            if trend is None or r is None:
                return
            pos = ctx.position(bar.symbol)
            qty = max(1, int(ctx.equity * 0.02 / bar.close))
            if pos is None and trend > bar.close:
                ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
            elif pos is not None and r > 70:
                ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
            elif pos is not None and bar.close < pos.entry_price * 0.95:
                ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
    """
)


def test_check1_passes_when_indicators_read_via_ctx_indicator() -> None:
    results = CodeConformanceGate().check(_CTX_INDICATOR_CODE, _ema_rsi_spec())
    assert _critical_details(results) == [], _critical_details(results)


def test_check1_accepts_name_keyword_form() -> None:
    code = _CTX_INDICATOR_CODE.replace(
        "ctx.indicator('ema', period=20)", "ctx.indicator(name='ema', period=20)"
    ).replace("ctx.indicator('rsi', period=14)", "ctx.indicator(name='rsi', period=14)")
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    indicator_criticals = [d for d in _critical_details(results) if "indicator(s)" in d]
    assert indicator_criticals == []


def test_check1_still_fails_when_indicator_not_read_at_all() -> None:
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    missing = [d for d in _critical_details(results) if "indicator(s)" in d]
    assert missing, "expected an indicator-presence critical"
    assert "ema" in missing[0] and "rsi" in missing[0]


def test_check1_ignores_ctx_indicator_in_unreachable_helper() -> None:
    # ctx.indicator reads live only in a helper on_bar never calls → not credited.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def _dead(self, ctx):
                return ctx.indicator('ema', period=20), ctx.indicator('rsi', period=14)

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    missing = [d for d in _critical_details(results) if "indicator(s)" in d]
    assert missing and "ema" in missing[0] and "rsi" in missing[0]


def test_check1_ignores_non_literal_indicator_name() -> None:
    # A computed indicator name cannot be statically matched to a spec indicator.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                which = 'ema'
                trend = ctx.indicator(which, period=20)
                r = ctx.indicator('rsi', period=14)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    missing = [d for d in _critical_details(results) if "indicator(s)" in d]
    assert missing and "ema" in missing[0]
    assert "rsi" not in missing[0]  # rsi read via a literal name → credited


def test_check1_still_accepts_legacy_named_call_form() -> None:
    # The deterministic compiler still emits self.sma(...); ensure it passes.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                bars = ctx.history(bar.symbol, 30)
                trend = self.ema(bars, 20)
                r = self.rsi(bars, 14)
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")

            def ema(self, bars, period):
                return bars[-1].close

            def rsi(self, bars, period):
                return 50.0
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    indicator_criticals = [d for d in _critical_details(results) if "indicator(s)" in d]
    assert indicator_criticals == []
