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
from investment_team.strategy_lab.executor.strategy_indicators import (
    bollinger_bands,
    indicator_value,
)
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


def _make_diverging_bars(n: int, *, symbol: str, seed: int) -> list[Bar]:
    """Like :func:`_make_bars`, but with a caller-chosen ``seed`` so two
    symbols' fixtures are genuinely different series.

    ``_make_bars`` reseeds ``random.Random(7)`` identically regardless of its
    ``symbol`` argument, so two calls with different symbols produce
    bit-identical OHLCV — fine for testing dispatch-by-symbol, but unable to
    distinguish real cross-symbol cache isolation from lucky coincidence
    (see ``test_ctx_indicator_isolates_deque_state_across_genuinely_different_symbols``).
    """
    rng = random.Random(seed)
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
# indicator_value — shared registry (issue: backtest hot loop caching)
# ---------------------------------------------------------------------------


def test_indicator_value_shares_one_registry_per_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """Acceptance criterion: the registry is instantiated once per backtest
    (per symbol), not once per indicator call — checked across many calls and
    two different indicators (one always-recompute, one deque-stateful).
    Passes an explicit ``registries`` dict, matching how ``ctx.indicator()``
    actually calls this in production (see ``contract.py``/
    ``predicate_conformance.py``) — ``indicator_value`` has no caching of its
    own for a caller that omits ``registries`` entirely; see
    ``test_ad_hoc_sequential_calls_with_overlapping_timestamps_no_longer_corrupt``
    in ``test_strategy_indicators.py`` for why that's deliberate.

    Reads a fixed-size sliding window each call (matching ``StrategyContext``
    ``_ingest_bar``'s bounded retention window, not an ever-growing prefix):
    the registry cache is also keyed by window length (see
    ``test_shared_registry_does_not_blend_different_window_depths_for_one_
    symbol`` in ``test_strategy_indicators.py``), so a history that never
    stopped growing would never actually settle on one registry either, in
    production or here — ``ctx.indicator()``'s own ``history`` argument
    always caps at ``STREAMING_WINDOW_BARS`` for exactly this reason.
    """
    from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry

    constructed: list[object] = []
    real_init = IndicatorRegistry.__init__

    def _counting_init(self) -> None:
        constructed.append(self)
        real_init(self)

    monkeypatch.setattr(IndicatorRegistry, "__init__", _counting_init)

    registries: dict = {}
    bars = _make_bars(n=40, symbol="QQQ")
    window = 20
    for i in range(window, len(bars) + 1):
        recent = bars[i - window : i]
        indicator_value("sma", recent, period=10, registries=registries)
        indicator_value("macd", recent, registries=registries)

    assert len(constructed) == 1


def test_indicator_value_registry_count_scales_with_distinct_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'Once per backtest' means once per symbol-stream within a backtest, not
    a single flat instance: a multi-symbol backtest constructs one registry
    per symbol it actually reads — still far below one-per-call, but not a
    literal singleton either. Passes an explicit ``registries`` dict and a
    fixed-size sliding window — see
    ``test_indicator_value_shares_one_registry_per_symbol`` above."""
    from investment_team.strategy_lab.indicators.streaming import IndicatorRegistry

    constructed: list[object] = []
    real_init = IndicatorRegistry.__init__

    def _counting_init(self) -> None:
        constructed.append(self)
        real_init(self)

    monkeypatch.setattr(IndicatorRegistry, "__init__", _counting_init)

    registries: dict = {}
    aaa = _make_bars(n=30, symbol="AAA")
    bbb = _make_bars(n=30, symbol="BBB")
    window = 20
    for i in range(window, 31):
        indicator_value("sma", aaa[i - window : i], period=10, registries=registries)
        indicator_value("sma", bbb[i - window : i], period=10, registries=registries)

    assert len(constructed) == 2


def test_indicator_value_recomputes_when_trailing_close_coincidentally_matches() -> None:
    """Regression test for a real bug caught in code review: two calls for
    the same symbol whose windows have the same length and the same final
    close value, but genuinely different earlier bars and a different final
    timestamp, must not let the shared registry return the first call's
    cached value for the second.

    Guards against IndicatorRegistry's ``(id(last), len, timestamp, close)``
    fingerprint colliding: ``_RegBar`` objects are freshly built and
    discarded every call, and CPython commonly reuses a just-freed object's
    ``id()`` for the next same-sized object — without a real, distinct
    timestamp on each row, that coincidence alone could make a genuinely
    different window look like a cache hit (or, for a deque-stateful
    indicator, look like the same stream advancing by one bar).
    """
    period = 10
    # 20 bars each; the trailing `period` window differs in every earlier
    # value but both sequences share the same final close (150.0) and the
    # same length — the exact ambiguity flagged in review.
    seq_a = [
        Bar(
            symbol="TEST",
            timestamp=f"2024-01-{i + 1:02d}T00:00:00Z",
            timeframe="1d",
            open=100.0,
            high=101.0,
            low=99.0,
            close=(100.0 + i if i < 19 else 150.0),
            volume=1000.0,
        )
        for i in range(20)
    ]
    seq_b = [
        Bar(
            symbol="TEST",
            timestamp=f"2024-06-{i + 1:02d}T00:00:00Z",
            timeframe="1d",
            open=200.0,
            high=201.0,
            low=199.0,
            close=(200.0 - i if i < 19 else 150.0),
            volume=1000.0,
        )
        for i in range(20)
    ]
    assert len(seq_a) == len(seq_b)
    assert seq_a[-1].close == seq_b[-1].close == 150.0
    assert seq_a[-1].timestamp != seq_b[-1].timestamp

    got_a = indicator_value("sma", seq_a, period=period)
    got_b = indicator_value("sma", seq_b, period=period)

    expected_a = sum(b.close for b in seq_a[-period:]) / period
    expected_b = sum(b.close for b in seq_b[-period:]) / period
    assert expected_a != expected_b  # the two windows must genuinely differ
    assert got_a == pytest.approx(expected_a)
    assert got_b == pytest.approx(expected_b)


def test_indicator_value_source_bucket_isolates_high_and_close_projections() -> None:
    """Regression test for a second bug caught in code review:
    ``indicator_value`` always dispatches to the registry with the literal
    ``source="close"`` (the caller's requested source is pre-projected onto
    ``_RegBar.close`` before the registry ever sees it), so the registry's
    own cache key can't tell "sma of high" apart from "sma of close" for the
    same bars. Engineered so the trailing bar's ``high`` equals its
    ``close`` (the exact scenario flagged in review) — without bucketing by
    the true requested source, the two projections' fingerprints could
    coincide entirely and one query would silently return the other's value.
    """
    n = 20
    bars = [
        Bar(
            symbol="TEST",
            timestamp=f"2024-01-{i + 1:02d}T00:00:00Z",
            timeframe="1d",
            open=100.0,
            high=100.0 + i + (0.0 if i == n - 1 else 5.0),
            low=99.0,
            close=100.0 + i,
            volume=1000.0,
        )
        for i in range(n)
    ]
    assert bars[-1].high == bars[-1].close  # the exact ambiguity flagged in review
    assert any(b.high != b.close for b in bars[:-1])  # earlier bars genuinely differ

    got_high = indicator_value("sma", bars, source="high", period=10)
    got_close = indicator_value("sma", bars, source="close", period=10)

    expected_high = sum(b.high for b in bars[-10:]) / 10
    expected_close = sum(b.close for b in bars[-10:]) / 10
    assert expected_high != expected_close  # the two sources must genuinely differ
    assert got_high == pytest.approx(expected_high)
    assert got_close == pytest.approx(expected_close)


def test_shadow_context_owns_isolated_indicator_registries() -> None:
    """Regression test for a third bug caught in code review: ``_ShadowContext``
    runs in-process on worker threads (e.g. ``api.main``'s
    ``_strategy_lab_worker`` ``ThreadPoolExecutor``) that can process many
    unrelated shadow-conformance executions over their lifetime. Each
    instance owns its own indicator-registry cache as an instance attribute
    (not shared thread-local state), so constructing a second context can
    neither read nor clear the first's — a stronger guarantee than "reset at
    construction" (see ``test_interleaved_contexts_do_not_corrupt_each_others_
    indicator_state`` for why construction-time-only resets aren't enough).
    """
    first = _ShadowContext()
    for i, b in enumerate(_shadow_bars(30)):
        first._ingest_bar(b, i)
    first.indicator("bollinger", period=20, band="upper")  # warms the cache for "QQQ"
    assert first._indicator_registries  # sanity: something got cached

    second = _ShadowContext()
    assert second._indicator_registries == {}  # a fresh, independent dict
    assert first._indicator_registries  # constructing `second` didn't touch `first`


def test_strategy_context_owns_isolated_indicator_registries() -> None:
    """``StrategyContext`` can be constructed in-process (not just inside the
    sandboxed subprocess — e.g. by tests); it gets the same per-instance
    isolation guarantee as ``_ShadowContext`` (see
    ``test_shadow_context_owns_isolated_indicator_registries``)."""
    first = StrategyContext(emit=lambda _d: None)
    for b in _make_bars(30, symbol="QQQ"):
        first._ingest_bar(b)
    first.indicator("bollinger", period=20, band="upper")
    assert first._indicator_registries

    second = StrategyContext(emit=lambda _d: None)
    assert second._indicator_registries == {}
    assert first._indicator_registries


def test_interleaved_contexts_do_not_corrupt_each_others_indicator_state() -> None:
    """Regression test for a fourth bug caught in code review: two contexts
    for the *same* symbol, constructed before either runs, with their bar
    ingestion and indicator reads interleaved bar-by-bar — not one context
    fully driven to completion before the next is even constructed. A
    thread-local cache keyed only by ``(symbol, source)`` cannot tell these
    two apart (both are "the same thread, the same symbol"), so an earlier
    "reset the cache at construction" fix does not help once interleaving
    begins — each context owning its own registries dict does not need to
    tell them apart at all. Uses ``bollinger`` (deque-stateful, so a
    corrupted read would visibly diverge from an independent computation)
    over two genuinely different series for the same symbol.
    """
    a = _make_diverging_bars(60, symbol="X", seed=1)
    b = _make_diverging_bars(60, symbol="X", seed=99)
    ctx_a = StrategyContext(emit=lambda _d: None)
    ctx_b = StrategyContext(emit=lambda _d: None)  # constructed before either runs
    for ba, bb in zip(a, b):
        ctx_a._ingest_bar(ba)
        ctx_a.indicator("bollinger", period=20, band="upper")
        ctx_b._ingest_bar(bb)
        ctx_b.indicator("bollinger", period=20, band="upper")

    got_a = ctx_a.indicator("bollinger", period=20, band="upper")
    got_b = ctx_b.indicator("bollinger", period=20, band="upper")
    exp_a = _engine_latest(
        _engine_view(a), IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"})
    )
    exp_b = _engine_latest(
        _engine_view(b), IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"})
    )
    assert got_a == pytest.approx(exp_a)
    assert got_b == pytest.approx(exp_b)
    assert got_a != pytest.approx(got_b)  # fixtures must actually differ


def test_interleaved_standalone_wrapper_calls_do_not_corrupt_each_others_indicator_state() -> None:
    """Regression test for a sixth bug caught in code review: the fourth
    bug's fix (instance-owned ``_indicator_registries``, see
    ``test_interleaved_contexts_do_not_corrupt_each_others_indicator_state``)
    only covers ``ctx.indicator(...)``. The 16 standalone wrapper functions
    have no ``registries`` parameter, so a strategy calling e.g.
    ``bollinger_bands(...)`` directly (a documented, supported call shape —
    see ``strategy_indicators``'s module docstring) always fell through to a
    cache *shared* with whatever other execution last ran on this thread,
    regardless of which context's dispatch was driving it.

    This is not a theoretical risk: sharing one registry across two
    different bar streams for the same symbol reliably corrupts
    deque-stateful indicators like ``bollinger`` — verified empirically
    (~91% of trials returned a value blended from both streams, via
    ``streaming.py``'s ``_advance_kind`` misclassifying the second stream's
    tail as a "slide" continuation of the first's cached deque state).

    ``_active_registries`` (a contextvar) closes this: ``StrategyContext``/
    ``_ShadowContext`` bracket every call into strategy code with
    ``.set(self._indicator_registries)``/``.reset(token)`` (see
    ``streaming_harness.py``'s ``_HARNESS_SCRIPT`` and
    ``predicate_conformance.py``'s ``_check_fixture``), so a standalone
    wrapper resolves to the *dispatching* context's own dict instead. This
    test brackets manually to prove the underlying mechanism directly, the
    same way the fourth bug's regression test manually interleaves two
    contexts even though no current caller genuinely interleaves them.
    """
    from investment_team.strategy_lab.executor import strategy_indicators as si

    a = _make_diverging_bars(60, symbol="X", seed=1)
    b = _make_diverging_bars(60, symbol="X", seed=99)
    ctx_a = StrategyContext(emit=lambda _d: None)
    ctx_b = StrategyContext(emit=lambda _d: None)  # constructed before either runs

    got_a = got_b = None
    for i in range(20, 61):
        token = si._active_registries.set(ctx_a._indicator_registries)
        try:
            got_a = bollinger_bands(a[:i], period=20)[0]  # (upper, middle, lower)
        finally:
            si._active_registries.reset(token)

        token = si._active_registries.set(ctx_b._indicator_registries)
        try:
            got_b = bollinger_bands(b[:i], period=20)[0]
        finally:
            si._active_registries.reset(token)

    exp_a = _engine_latest(
        _engine_view(a), IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"})
    )
    exp_b = _engine_latest(
        _engine_view(b), IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"})
    )
    assert got_a == pytest.approx(exp_a)
    assert got_b == pytest.approx(exp_b)
    assert got_a != pytest.approx(got_b)  # fixtures must actually differ


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
        indicator_value("vwap", bars, periodd=14)  # vwap takes only 'period'
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


def test_ctx_indicator_isolates_deque_state_across_genuinely_different_symbols() -> None:
    """Regression guard for cross-symbol cache collisions in the shared
    IndicatorRegistry.

    Unlike ``test_strategy_context_indicator_multi_symbol_isolation`` (whose
    two symbols' fixtures are numerically identical, so it can't distinguish
    real isolation from lucky coincidence), this uses genuinely divergent
    series for ``bollinger`` — one of ``IndicatorRegistry``'s deque-stateful
    methods with no ``symbol`` component in its own cache key — so a shared,
    unbucketed registry would produce a visibly wrong value for at least one
    symbol once its state gets interleaved with the other's.
    """
    a = _make_diverging_bars(60, symbol="AAA", seed=1)
    b = _make_diverging_bars(60, symbol="BBB", seed=99)
    ctx = StrategyContext(emit=lambda _d: None)
    for ba, bb in zip(a, b):
        ctx._ingest_bar(ba)
        ctx._ingest_bar(bb)

    got_aaa = ctx.indicator("bollinger", period=20, band="upper", symbol="AAA")
    got_bbb = ctx.indicator("bollinger", period=20, band="upper", symbol="BBB")
    exp_aaa = _engine_latest(
        _engine_view(a), IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"})
    )
    exp_bbb = _engine_latest(
        _engine_view(b), IndicatorRef(name="bollinger", params={"period": 20, "band": "upper"})
    )
    assert got_aaa == pytest.approx(exp_aaa)
    assert got_bbb == pytest.approx(exp_bbb)
    assert got_aaa != pytest.approx(got_bbb)  # fixtures must actually differ


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
        "ctx.indicator('ema', period=20)",  # valid — matches the spec's ema ref exactly
    ],
)
def test_check1_flags_malformed_ctx_indicator_call(ema_call) -> None:
    # A fully-literal, statically-invalid ctx.indicator read is a critical here,
    # so it is refined rather than raising at runtime. (The third case matches the
    # spec's ``ema`` ref exactly — period 20, default 'close' source — and must NOT
    # be flagged by the malformed-call or the spec-divergence checker.)
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
    if "period=20)" in ema_call and "perod" not in ema_call:
        assert malformed == []  # matches the spec ref exactly → valid
    else:
        assert malformed and "ema" in malformed[0]


def test_ctx_indicator_source_divergence_from_spec_is_critical() -> None:
    # The spec's ``ema`` ref uses the default 'close' source; reading it on 'high'
    # would execute trades that do not implement the specification. The faithfulness
    # checker flags the divergence so the code is refined before it ever runs.
    code = _CTX_INDICATOR_CODE.replace(
        "ctx.indicator('ema', period=20)", "ctx.indicator('ema', period=20, source='high')"
    )
    results = CodeConformanceGate().check(code, _ema_rsi_spec())
    diverged = [
        d for d in _critical_details(results) if "diverges from the spec" in d and "source=" in d
    ]
    assert diverged, _critical_details(results)
    assert "ema" in diverged[0]


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
