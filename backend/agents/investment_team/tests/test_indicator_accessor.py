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
    with pytest.raises(ValueError, match="expected one of"):
        indicator_value(history=_make_bars(), **kwargs)


def test_indicator_value_bad_source_raises() -> None:
    with pytest.raises(ValueError, match="unknown indicator source"):
        indicator_value("sma", _make_bars(), period=10, source="median")


@pytest.mark.parametrize("name", ["atr", "adx", "stochastic", "vwap"])
def test_indicator_value_ohlc_indicator_rejects_source_override(name) -> None:
    bars = _make_bars()
    # A non-default source on an OHLC indicator is a contract violation (these
    # read OHLC directly), so it must raise rather than silently mis-source.
    with pytest.raises(ValueError, match="does not accept a 'source' override"):
        indicator_value(name, bars, source="high")
    # The default (no source override) still computes normally.
    assert indicator_value(name, bars) is not None


def test_indicator_value_rejects_unexpected_param() -> None:
    bars = _make_bars()
    # An unknown/typo'd param must raise rather than silently using defaults —
    # matching IndicatorRef strictness for reads that reach runtime (e.g. via
    # **kwargs the static gate cannot inspect).
    with pytest.raises(ValueError, match="unexpected param"):
        indicator_value("rsi", bars, perod=14)
    with pytest.raises(ValueError, match="unexpected param"):
        indicator_value("vwap", bars, period=14)  # vwap takes no params
    with pytest.raises(ValueError, match="unexpected param"):
        indicator_value("macd", bars, fast=12, slo=26)
    # Correctly-spelled params still compute.
    assert indicator_value("rsi", bars, period=14) is not None


def test_indicator_value_validates_param_types_and_ranges() -> None:
    bars = _make_bars()
    # Out-of-DSL values are contract violations even for dynamic params the
    # static gate cannot inspect — they must raise rather than coerce via int().
    for bad in (1.5, "20", 1, 9999):  # non-int / str / below-min / above-max
        with pytest.raises(ValueError):
            indicator_value("sma", bars, period=bad)
    with pytest.raises(ValueError):
        indicator_value("bollinger", bars, num_std=0)  # must be > 0
    with pytest.raises(ValueError):
        indicator_value("macd", bars, output="nope")  # invalid selector
    # Valid params still compute.
    assert indicator_value("sma", bars, period=20) is not None
    assert indicator_value("bollinger", bars, num_std=2.5, band="upper") is not None


def test_indicator_value_rejects_non_finite_num_std() -> None:
    # inf/nan pass a naive `> 0` check but are DSL contract violations (the spec
    # requires finite floats); they must raise rather than yield infinite bands.
    bars = _make_bars()
    for bad in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ValueError):
            indicator_value("bollinger", bars, num_std=bad)


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


def test_shadow_context_indicator_no_bar_raises_then_warmup_none() -> None:
    shadow = _ShadowContext()
    # No bar dispatched yet → mirror the real StrategyContext.indicator ValueError.
    with pytest.raises(ValueError, match="no bar has been dispatched"):
        shadow.indicator("sma", period=20)
    shadow._ingest_bar(_shadow_bars(1)[0], 0)
    assert shadow.indicator("sma", period=20) is None  # warm-up


def test_shadow_context_indicator_explicit_unknown_symbol_returns_none() -> None:
    shadow = _ShadowContext()
    for i, b in enumerate(_shadow_bars(10)):
        shadow._ingest_bar(b, i)
    assert shadow.indicator("sma", period=5, symbol="ZZZ") is None


def test_last_or_none_handles_empty_series() -> None:
    import pandas as pd

    from investment_team.strategy_lab.executor.strategy_indicators import _last_or_none

    assert _last_or_none(pd.Series([], dtype=float)) is None


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


def test_check1_requires_ctx_receiver_for_indicator_credit() -> None:
    # self.indicator(...) / foo.indicator(...) are NOT the engine-backed accessor
    # and must not satisfy the presence check.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = self.indicator('ema', period=20)
                r = self.indicator('rsi', period=14)
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
    assert missing and "ema" in missing[0] and "rsi" in missing[0]


@pytest.mark.parametrize(
    "ema_call",
    [
        "ctx.indicator('ema')",  # missing required period
        "ctx.indicator('ema', perod=20)",  # typo'd param
        "ctx.indicator('ema', period=20, source='high')",  # valid — ema accepts source
    ],
)
def test_check1_flags_malformed_ctx_indicator_call(ema_call) -> None:
    # A fully-literal, statically-invalid ctx.indicator read is a critical here,
    # so it is refined rather than raising at runtime. (The third case is valid
    # — ema accepts source — and must NOT be flagged.)
    code = textwrap.dedent(
        f"""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({{"QQQ"}})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = {ema_call}
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
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    malformed = [d for d in _critical_details(results) if "ctx.indicator(" in d]
    if "source='high'" in ema_call:
        assert malformed == []  # ema accepts a source override → valid
    else:
        assert malformed and "ema" in malformed[0]


def test_check1_skips_validation_for_dynamic_ctx_indicator_args() -> None:
    # A dynamic param (period=self.WINDOW) cannot be validated statically, so it
    # must not be falsely flagged — and the read is still credited for presence.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})
            WINDOW = 20

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', period=self.WINDOW)
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
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    assert _critical_details(results) == [], _critical_details(results)


def test_check1_validates_params_when_symbol_is_dynamic() -> None:
    # A dynamic `symbol` must not stop static param validation: it is not part of
    # the indicator contract, so ctx.indicator('ema', symbol=bar.symbol) is still
    # caught as missing `period`, while a well-formed rsi read with a dynamic
    # symbol is not flagged.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', symbol=bar.symbol)
                r = ctx.indicator('rsi', period=14, symbol=bar.symbol)
                if trend is None or r is None:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    malformed = [d for d in _critical_details(results) if "ctx.indicator(" in d]
    assert malformed and "ema" in malformed[0]
    assert all("rsi" not in d for d in malformed)


def test_check1_flags_positional_ctx_indicator_param() -> None:
    # ctx.indicator(...) is keyword-only after the name; ctx.indicator('ema', 20)
    # is a guaranteed runtime TypeError and must be flagged, not credited.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', 20)
                r = ctx.indicator('rsi', period=14)
                if trend is None or r is None:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    positional = [d for d in _critical_details(results) if "positionally" in d]
    assert positional and "ema" in positional[0]


def test_check1_flags_duplicate_indicator_name() -> None:
    # ctx.indicator('ema', name='ema', ...) gives `name` twice → runtime TypeError.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', name='ema', period=20)
                r = ctx.indicator('rsi', period=14)
                if trend is None or r is None:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    dup = [d for d in _critical_details(results) if "as name=" in d]
    assert dup and "ema" in dup[0]


def test_check1_flags_typo_param_even_with_dynamic_sibling_value() -> None:
    # A dynamic value (period=self.WINDOW) must not suppress detection of the
    # statically-known typo'd key `perod`.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})
            WINDOW = 20

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', period=self.WINDOW, perod=20)
                r = ctx.indicator('rsi', period=14)
                if trend is None or r is None:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    unexpected = [d for d in _critical_details(results) if "perod" in d]
    assert unexpected and "unexpected param" in unexpected[0]


def test_check1_flags_unknown_ctx_indicator_name() -> None:
    # An extra read of an unknown indicator is a runtime ValueError; flag it
    # statically even when the required spec indicators are read correctly.
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', period=20)
                r = ctx.indicator('rsi', period=14)
                custom = ctx.indicator('supertrend', period=10)
                if trend is None or r is None or custom is None:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    unknown = [d for d in _critical_details(results) if "unknown indicator" in d]
    assert unknown and "supertrend" in unknown[0]


def test_check1_flags_invalid_source_literal() -> None:
    # A source-aware indicator with an invalid literal source raises at runtime;
    # flag it statically. (A valid source like 'high' must NOT be flagged — see
    # test_check1_flags_malformed_ctx_indicator_call.)
    code = textwrap.dedent(
        """
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({"QQQ"})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', period=20, source='median')
                r = ctx.indicator('rsi', period=14)
                if trend is None or r is None:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    bad_source = [d for d in _critical_details(results) if "invalid source" in d]
    assert bad_source and "median" in bad_source[0]


def _spec_code_reading(symbol_literal: str) -> str:
    return textwrap.dedent(
        f"""
        from contract import Strategy

        class S(Strategy):
            UNIVERSE = frozenset({{"QQQ"}})

            def on_bar(self, ctx, bar):
                if bar.symbol not in self.UNIVERSE:
                    return
                trend = ctx.indicator('ema', period=20, symbol={symbol_literal!r})
                r = ctx.indicator('rsi', period=14)
                if trend is None or r is None:
                    return
                pos = ctx.position(bar.symbol)
                qty = max(1, int(ctx.equity * 0.02 / bar.close))
                if pos is None and trend > bar.close:
                    ctx.submit_order(symbol=bar.symbol, qty=qty, side="LONG")
                elif pos is not None and r > 70:
                    ctx.submit_order(symbol=bar.symbol, qty=pos.qty, side="SHORT")
        """
    )


def test_check1_flags_foreign_symbol_literal() -> None:
    # A literal symbol outside the spec's target_symbols (QQQ) never receives
    # data, so the indicator read is dead — flag it, don't credit presence.
    results = CodeConformanceGate().check(_spec_code_reading("SPY"), _ema_rsi_spec())
    foreign = [d for d in _critical_details(results) if "target_symbols" in d and "SPY" in d]
    assert foreign


def test_check1_allows_in_universe_symbol_literal() -> None:
    # A literal symbol that IS in target_symbols is fine.
    results = CodeConformanceGate().check(_spec_code_reading("QQQ"), _ema_rsi_spec())
    foreign = [d for d in _critical_details(results) if "target_symbols" in d]
    assert foreign == []
