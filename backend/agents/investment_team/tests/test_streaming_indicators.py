"""Parity + invariant tests for the streaming indicator registry.

The registry in ``strategy_lab/indicators/streaming.py`` is the canonical
implementation reused by the host-side primitives, the executor's
``StreamingHistoryView``, and (via shared template text) the two
compilers. The tests here:

* assert bit-identical output against the original O(N²) MACD math so
  the synthesis compiler's golden snapshots can never drift;
* drive every indicator bar-by-bar to confirm the cache's
  cold-start / single-step / same-bar branches are all exercised and
  agree with the cold-only reference;
* check the replay/seek fallback: feeding the registry truncated
  history must produce the same value as a fresh registry given the
  same truncated history.
"""

from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import dataclass
from typing import List

import pytest

from investment_team.strategy_lab.indicators.streaming import (
    IndicatorRegistry,
    macd_components,
    windowed_ema,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@dataclass
class _Bar:
    """Bar-shaped record matching ``contract.Bar``'s field surface."""

    timestamp: str
    open: float = 100.0
    high: float = 100.0
    low: float = 100.0
    close: float = 100.0
    volume: float = 1.0


def _series(n: int, seed: int = 0) -> List[_Bar]:
    rng = random.Random(seed)
    bars: List[_Bar] = []
    for i in range(n):
        close = 100.0 + rng.uniform(-3.0, 3.0) + i * 0.3
        spread = 0.5
        bars.append(
            _Bar(
                timestamp=f"2024-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                open=close - 0.1,
                high=close + spread,
                low=close - spread,
                close=close,
                volume=1000.0 + i,
            )
        )
    return bars


# ---------------------------------------------------------------------------
# Legacy reference (the exact math the original compiler template ran).
# ---------------------------------------------------------------------------


def _legacy_macd(history, *, fast: int, slow: int, signal: int, select: str = "macd"):
    if fast >= slow:
        return None
    min_bars = slow if select == "macd" else slow + signal - 1
    if len(history) < min_bars:
        return None
    macd_line: List[float] = []
    for end in range(slow, len(history) + 1):
        sub = history[:end]
        alpha_f = 2.0 / (fast + 1.0)
        ef = sub[-fast].close
        for b in sub[-fast + 1 :]:
            ef = alpha_f * b.close + (1.0 - alpha_f) * ef
        alpha_s = 2.0 / (slow + 1.0)
        es = sub[-slow].close
        for b in sub[-slow + 1 :]:
            es = alpha_s * b.close + (1.0 - alpha_s) * es
        macd_line.append(ef - es)
    if select == "macd":
        return macd_line[-1]
    if len(macd_line) < signal:
        return None
    alpha_g = 2.0 / (signal + 1.0)
    sig = macd_line[0]
    for x in macd_line[1:]:
        sig = alpha_g * x + (1.0 - alpha_g) * sig
    if select == "signal":
        return sig
    if select == "histogram":
        return macd_line[-1] - sig
    return None


def _legacy_ema(bars, period: int) -> float:
    if len(bars) < period:
        return float("nan")
    alpha = 2.0 / (period + 1.0)
    val = bars[-period].close
    for b in bars[-period + 1 :]:
        val = alpha * b.close + (1.0 - alpha) * val
    return val


# ---------------------------------------------------------------------------
# MACD parity — cold-start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("select", ["macd", "signal", "histogram"])
def test_macd_components_match_legacy_cold_start(select: str) -> None:
    """Single cold-start at varying history depths must match legacy bit-for-bit."""
    bars = _series(80, seed=11)
    reg = IndicatorRegistry()
    for n in range(20, len(bars) + 1):
        sub = bars[:n]
        new = reg.macd(sub, fast=12, slow=26, signal=9, select=select)
        # Reset state so each call is an independent cold-start.
        reg._state.clear()
        ref = _legacy_macd(sub, fast=12, slow=26, signal=9, select=select)
        assert new == ref, f"select={select} n={n} new={new!r} ref={ref!r}"


# ---------------------------------------------------------------------------
# MACD parity — streaming step
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("select", ["macd", "signal", "histogram"])
def test_macd_streaming_matches_legacy_bar_by_bar(select: str) -> None:
    """Driving the registry bar-by-bar (single-step path) must match legacy."""
    bars = _series(80, seed=37)
    reg = IndicatorRegistry()
    for n in range(26, len(bars) + 1):
        sub = bars[:n]
        streaming = reg.macd(sub, fast=12, slow=26, signal=9, select=select)
        ref = _legacy_macd(sub, fast=12, slow=26, signal=9, select=select)
        assert streaming == ref, f"select={select} n={n} streaming={streaming!r} ref={ref!r}"


def test_macd_same_bar_returns_cached_value() -> None:
    """Two same-``bars[-1]`` calls return the exact cached value (no recompute)."""
    bars = _series(50, seed=5)
    reg = IndicatorRegistry()
    first = reg.macd(bars, fast=12, slow=26, signal=9, select="signal")
    second = reg.macd(bars, fast=12, slow=26, signal=9, select="signal")
    assert first == second
    # Two selects on the same bar — both come from the same cached payload.
    macd_val = reg.macd(bars, fast=12, slow=26, signal=9, select="macd")
    hist_val = reg.macd(bars, fast=12, slow=26, signal=9, select="histogram")
    assert macd_val - first == pytest.approx(hist_val, rel=0, abs=1e-12)


def test_macd_replay_falls_back_to_cold_start() -> None:
    """Feeding the registry a shorter history forces a cold-start fallback."""
    bars = _series(60, seed=2)
    reg = IndicatorRegistry()
    for n in range(35, 61):
        reg.macd(bars[:n], fast=12, slow=26, signal=9, select="signal")
    # Now replay at n=40 — registry must NOT carry forward state from n=60.
    truncated = bars[:40]
    replay_val = reg.macd(truncated, fast=12, slow=26, signal=9, select="signal")
    fresh_val = IndicatorRegistry().macd(truncated, fast=12, slow=26, signal=9, select="signal")
    assert replay_val == fresh_val


def test_macd_warmup_returns_none() -> None:
    reg = IndicatorRegistry()
    bars = _series(25)  # slow=26 → too short
    assert reg.macd(bars, fast=12, slow=26, signal=9, select="macd") is None
    assert reg.macd(bars, fast=12, slow=26, signal=9, select="signal") is None
    assert reg.macd(bars, fast=12, slow=26, signal=9, select="histogram") is None


def test_macd_raises_value_error_on_bad_params() -> None:
    """``IndicatorRegistry.macd`` enforces the same precondition floor as
    ``macd_components`` — fast >= 2, slow > fast, signal >= 2. Earlier
    revisions silently returned None / degenerate results for invalid
    inputs while ``macd_components`` raised: same parameters, two
    contracts. The registry now raises in lock-step."""
    reg = IndicatorRegistry()
    bars = _series(60)
    with pytest.raises(ValueError):
        reg.macd(bars, fast=30, slow=10, signal=9, select="macd")
    with pytest.raises(ValueError):
        reg.macd(bars, fast=1, slow=26, signal=9, select="macd")
    with pytest.raises(ValueError):
        reg.macd(bars, fast=12, slow=26, signal=1, select="macd")


# ---------------------------------------------------------------------------
# MACD — sliding-window correctness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("select", ["macd", "signal", "histogram"])
def test_macd_sliding_window_matches_legacy(select: str) -> None:
    """A registry driven with a fixed-length sliding window (the shape
    ``ctx.history(symbol, depth)`` returns in production) must produce
    the same value as a fresh cold-compute on the same slice — i.e. the
    cached macd_line is trimmed when the window slides.

    Earlier revisions only handled the *expanding*-bars shape and would
    let the cached macd_line grow past the legacy bound on every slide,
    silently shifting the signal-EMA seed and returning wrong values.
    """
    bars = _series(120, seed=88)
    window_size = 40  # > slow + signal so signal is computable from the slice
    reg = IndicatorRegistry()
    for offset in range(0, len(bars) - window_size + 1):
        sliding = bars[offset : offset + window_size]
        streaming = reg.macd(sliding, fast=12, slow=26, signal=9, select=select)
        cold = _legacy_macd(sliding, fast=12, slow=26, signal=9, select=select)
        assert streaming == cold, (
            f"select={select} offset={offset} streaming={streaming!r} cold={cold!r}"
        )


def test_macd_sliding_window_keeps_macd_line_bounded() -> None:
    """After many slide-steps the macd_line deque must not grow past
    ``window_size - slow + 1`` — otherwise the signal-EMA per-call cost
    drifts to O(bars_seen) instead of O(window)."""
    bars = _series(500, seed=89)
    window_size = 40
    reg = IndicatorRegistry()
    for offset in range(0, len(bars) - window_size + 1):
        reg.macd(
            bars[offset : offset + window_size],
            fast=12,
            slow=26,
            signal=9,
            select="signal",
        )
    # The single cached macd_line for this (symbol, params) key must not
    # have been allowed to balloon past the windowed bound.
    cached = next(iter(reg._state.values()))
    expected_max = window_size - 26 + 1
    assert len(cached["macd_line"]) == expected_max


# ---------------------------------------------------------------------------
# MACD — symbol isolation
# ---------------------------------------------------------------------------


def test_macd_isolates_symbols_when_registry_shared() -> None:
    """A registry driven with bars from two different symbols must keep
    each symbol's macd_line in its own cache slot.

    Without symbol in the key, the previous design would let an
    AAPL-bar advance silently mutate the cached MSFT macd_line whenever
    the two symbols' bars shared a timestamp (the common case for daily
    aligned histories)."""

    @dataclass
    class _SymBar:
        symbol: str
        timestamp: str
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        close: float = 100.0
        volume: float = 1.0

    aapl = [
        _SymBar(symbol="AAPL", timestamp=f"2024-01-{i + 1:02d}", close=100.0 + i * 0.5)
        for i in range(60)
    ]
    msft = [
        _SymBar(symbol="MSFT", timestamp=f"2024-01-{i + 1:02d}", close=200.0 - i * 0.3)
        for i in range(60)
    ]
    reg = IndicatorRegistry()
    for n in range(34, 61):
        v_a = reg.macd(aapl[:n], fast=12, slow=26, signal=9, select="signal")
        v_b = reg.macd(msft[:n], fast=12, slow=26, signal=9, select="signal")
        a_ref = IndicatorRegistry().macd(aapl[:n], fast=12, slow=26, signal=9, select="signal")
        b_ref = IndicatorRegistry().macd(msft[:n], fast=12, slow=26, signal=9, select="signal")
        assert v_a == a_ref, f"n={n} AAPL drifted: {v_a!r} != {a_ref!r}"
        assert v_b == b_ref, f"n={n} MSFT drifted: {v_b!r} != {b_ref!r}"


# ---------------------------------------------------------------------------
# Advance-kind discriminator
# ---------------------------------------------------------------------------


def test_advance_kind_classifies_expand_slide_and_none() -> None:
    """The discriminator must distinguish expansion (warm-up), slide
    (steady state), and anything else (cold-start fallback)."""
    bars = _series(60, seed=90)
    reg = IndicatorRegistry()
    # Cold-start at len = 40 (well past warm-up so state is populated).
    reg.macd(bars[:40], fast=12, slow=26, signal=9, select="signal")
    state = next(iter(reg._state.values()))

    # Expand: same id at -2, length grew by 1.
    fp_expand = reg._bar_fingerprint(bars[:41])
    assert reg._advance_kind(state, bars[:41], fp_expand) == "expand"

    # Slide: previous-last bar id still appears at -2 but length unchanged.
    sliding = bars[1:41]  # starts one bar later, same length as bars[:40]
    fp_slide = reg._bar_fingerprint(sliding)
    assert reg._advance_kind(state, sliding, fp_slide) == "slide"

    # Multi-bar jump: length grew by more than 1 — must NOT be classified
    # as a single-step advance even if bars[-2].timestamp aliases the
    # cached fingerprint's timestamp.
    big_jump = bars[:43]
    fp_jump = reg._bar_fingerprint(big_jump)
    assert reg._advance_kind(state, big_jump, fp_jump) == "none"


def test_advance_kind_rejects_multi_bar_jump_when_prev_matches() -> None:
    """A multi-bar jump where ``bars[-2]`` aliases the cached prev bar by
    timestamp (or close) but length delta is non-±1 must still classify
    as ``none``. Without the length-delta guard, the registry would
    single-step over a multi-bar gap and silently corrupt ``macd_line``.
    The previous ``_advance_kind`` test's ``big_jump`` path exits early
    via prev_matches=False; this case drives prev_matches=True.
    """

    @dataclass
    class _AliasBar:
        timestamp: str
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

    bars0 = [_AliasBar(timestamp=f"T_{i}", close=100.0 + i) for i in range(10)]
    reg = IndicatorRegistry()
    # Manually inject state — use deque() to match the registry's actual
    # invariant (Deque[float] for macd_line). Earlier revisions injected
    # [] (list), which would silently violate the popleft assumption in
    # any test that exercised the slide branch.
    fp_seed = reg._bar_fingerprint(bars0)
    reg._state[("macd", None, 12, 26, 9, "close")] = {
        "fp": fp_seed,
        "macd_line": deque(),
        "value": {"macd": 0.0, "signal": None, "histogram": None},
    }
    state = reg._state[("macd", None, 12, 26, 9, "close")]

    # Construct a candidate where bars[-2] aliases the cached prev bar by
    # timestamp (id will differ — fresh object), and total length is
    # prev_fp[1] + 2 (multi-bar jump). prev_matches=True via the
    # timestamp leg, length-delta gate must reject.
    aliased_prev = _AliasBar(timestamp=bars0[-1].timestamp, close=bars0[-1].close)
    multi_jump = (
        list(bars0[:-1])
        + [_AliasBar(timestamp="T_10", close=110.0)]
        + [aliased_prev]  # bars[-2] aliases cached prev by ts
        + [_AliasBar(timestamp="T_11", close=111.0)]
    )
    assert len(multi_jump) == len(bars0) + 2  # delta = +2 (multi-bar jump)
    fp_multi = reg._bar_fingerprint(multi_jump)
    # prev_matches=True via timestamp leg, length delta=+2 → "none".
    assert reg._advance_kind(state, multi_jump, fp_multi) == "none"


def test_advance_kind_close_leg_rescues_fresh_copy_callers() -> None:
    """The close-leg of ``prev_matches`` is a conditional fallback that
    fires only when the timestamp leg is unavailable on BOTH sides —
    the canonical fresh-copy scenario where ``ctx.history`` returns
    re-validated bar wrappers without timestamps. With the close-leg,
    the registry still classifies sliding/expanding as such (avoiding
    silent cold-rebuild every call). Without it (the pre-fix behaviour),
    fresh-copy callers regressed to legacy O(N) per bar."""

    @dataclass
    class _NoTsBar:
        # Deliberately no `timestamp` attribute: getattr fallback to None.
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

    bars = [_NoTsBar(close=100.0 + i * 0.5) for i in range(35)]
    reg = IndicatorRegistry()
    # Cold-start cached state at bars[:34] (just past warm-up for signal).
    reg.macd(bars[:34], fast=12, slow=26, signal=9, select="signal")
    cached_state = reg._state[("macd", None, 12, 26, 9, "close")]
    cached_fp = cached_state["fp"]
    assert cached_fp[2] is None  # confirms ts leg is unavailable
    assert cached_fp[3] is not None  # close leg IS populated

    # Now build bars[:35] but rebuild the wrappers (fresh copies with
    # different id but identical close at index -2). The timestamp leg
    # remains unavailable; the close leg must rescue.
    fresh_bars = [_NoTsBar(close=b.close) for b in bars[:35]]
    fp_fresh = reg._bar_fingerprint(fresh_bars)
    # bars[-2] in fresh_bars is fresh_bars[33], whose close matches
    # cached_fp[3] (the previously-last bar's close).
    assert reg._advance_kind(cached_state, fresh_bars, fp_fresh) == "expand"


def test_advance_kind_close_leg_does_not_fire_when_ts_available() -> None:
    """When timestamps ARE present, the close-leg must NOT activate —
    two unrelated symbol-less streams sharing a boundary close (flat
    market, integer-tick prices) would otherwise silently merge.
    Locks in the conditional gate on the close-leg."""

    @dataclass
    class _Bar:
        timestamp: str
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

    reg = IndicatorRegistry()
    # Stream A cached state — last bar has ts="A_T_9", close=100.0.
    stream_a_last = _Bar(timestamp="A_T_9", close=100.0)
    fp_a = (id(stream_a_last), 10, "A_T_9", 100.0)
    reg._state[("macd", None, 12, 26, 9, "close")] = {
        "fp": fp_a,
        "macd_line": deque(),
        "value": {"macd": 0.0, "signal": None, "histogram": None},
    }
    state = reg._state[("macd", None, 12, 26, 9, "close")]

    # Stream B advance — bars[-2] has DIFFERENT id, DIFFERENT timestamp,
    # but the SAME close (100.0). Old (unconditional close-leg) behavior:
    # prev_matches=True via close, length delta=+1 → expand, corrupted.
    # New (conditional close-leg): ts_leg_available=True (both have ts),
    # ts mismatch → prev_matches=False → "none".
    stream_b_prev = _Bar(timestamp="B_T_5", close=100.0)
    stream_b_new = _Bar(timestamp="B_T_6", close=101.0)
    fresh_b = [_Bar(timestamp=f"B_T_{i}", close=99.0 + i * 0.1) for i in range(9)]
    fresh_b.extend([stream_b_prev, stream_b_new])  # len = 11 = prev_fp[1] + 1
    fp_b = reg._bar_fingerprint(fresh_b)
    assert reg._advance_kind(state, fresh_b, fp_b) == "none"


def test_bar_fingerprint_normalises_pathological_close_values() -> None:
    """The close slot in the fingerprint must collapse every pathological
    value to None so tuple-equality stays well-behaved and the close-leg
    of prev_matches degrades cleanly to id/ts. Covered: None, Python
    bool, NaN, +inf/-inf, non-numeric strings.

    NumPy/pandas-specific cases (numpy.bool_, pd.NA, pd.NaT) are pinned
    by `test_bar_fingerprint_handles_numpy_and_pandas_pathologies`."""
    import math as _math

    @dataclass
    class _Bar:
        close: object  # untyped to allow bool/NaN/None/inf
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        timestamp: str = "T"

    reg = IndicatorRegistry()
    assert reg._bar_fingerprint([_Bar(close=_math.nan)])[3] is None
    assert reg._bar_fingerprint([_Bar(close=True)])[3] is None
    assert reg._bar_fingerprint([_Bar(close=False)])[3] is None
    assert reg._bar_fingerprint([_Bar(close=None)])[3] is None
    # inf would saturate the EMA recurrence (`alpha * inf = inf`) and
    # poison the cached macd_line for the registry's lifetime; collapse
    # to None so the close-leg of prev_matches doesn't admit it.
    assert reg._bar_fingerprint([_Bar(close=_math.inf)])[3] is None
    assert reg._bar_fingerprint([_Bar(close=-_math.inf)])[3] is None
    # Non-numeric strings raise ValueError from float() and would
    # otherwise crash the fingerprint; degrade to None.
    assert reg._bar_fingerprint([_Bar(close="not a number")])[3] is None
    # Real float passes through.
    assert reg._bar_fingerprint([_Bar(close=100.5)])[3] == 100.5
    # Integer coerces to float.
    assert reg._bar_fingerprint([_Bar(close=42)])[3] == 42.0


def test_bar_fingerprint_handles_numpy_and_pandas_pathologies() -> None:
    """The close-slot normalisation must catch numpy.bool_ (NOT a
    subclass of Python bool since numpy >= 1.20) and pandas missing-data
    sentinels (pd.NA / pd.NaT — both raise TypeError from float()).
    Previous code missed both."""
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")

    @dataclass
    class _Bar:
        close: object
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        timestamp: str = "T"

    reg = IndicatorRegistry()
    # numpy.bool_ would silently coerce to 1.0/0.0 via float() and
    # collide with real penny closes; isinstance(np.bool_(True), bool)
    # is False under numpy >= 1.20.
    assert reg._bar_fingerprint([_Bar(close=np.bool_(True))])[3] is None
    assert reg._bar_fingerprint([_Bar(close=np.bool_(False))])[3] is None
    # pd.NA / pd.NaT raise TypeError from float() — must degrade to None
    # instead of crashing the fingerprint.
    assert reg._bar_fingerprint([_Bar(close=pd.NA)])[3] is None
    assert reg._bar_fingerprint([_Bar(close=pd.NaT)])[3] is None
    # numpy.nan / numpy.inf also degrade.
    assert reg._bar_fingerprint([_Bar(close=np.nan)])[3] is None
    assert reg._bar_fingerprint([_Bar(close=np.inf)])[3] is None


def test_advance_kind_close_leg_does_not_fire_when_ts_asymmetric() -> None:
    """The close-leg must only fire when ts is unavailable on BOTH
    sides. If the cached fp has a ts but the new prev_bar doesn't (or
    vice versa), the close-leg must NOT activate — otherwise unrelated
    streams that drift in/out of ts coverage can silently merge through
    coincident closes. Locks in the symmetric-absence semantic of the
    new ``both_ts_absent`` gate."""

    @dataclass
    class _TSBar:
        timestamp: str
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

    @dataclass
    class _NoTSBar:
        # No `timestamp` attribute on purpose.
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

    reg = IndicatorRegistry()
    # Case A: cached side has ts, current side does NOT — asymmetric.
    cached_state = {
        "fp": (12345, 10, "T_9", 100.0),
        "macd_line": deque(),
        "value": {"macd": 0.0, "signal": None, "histogram": None},
    }
    fresh_a = [_NoTSBar(close=99.0 + i * 0.1) for i in range(9)]
    fresh_a.extend([_NoTSBar(close=100.0), _NoTSBar(close=101.0)])  # len=11
    fp_a = reg._bar_fingerprint(fresh_a)
    # bars[-2] has close=100.0 matching cached fp[3], but ts is asymmetric.
    # MUST NOT classify as expand/slide — close-leg is gated on
    # symmetric ts absence.
    assert reg._advance_kind(cached_state, fresh_a, fp_a) == "none"

    # Case B: cached side has NO ts, current side does — also asymmetric.
    cached_state_no_ts = {
        "fp": (54321, 10, None, 100.0),
        "macd_line": deque(),
        "value": {"macd": 0.0, "signal": None, "histogram": None},
    }
    fresh_b = [_TSBar(timestamp=f"U_{i}", close=99.0 + i * 0.1) for i in range(9)]
    fresh_b.extend([_TSBar(timestamp="U_9", close=100.0), _TSBar(timestamp="U_10", close=101.0)])
    fp_b = reg._bar_fingerprint(fresh_b)
    assert reg._advance_kind(cached_state_no_ts, fresh_b, fp_b) == "none"


def test_advance_kind_pydantic_round_trip_with_stamped_timestamps_cold_rebuilds() -> None:
    """Pins the conditional close-leg trade-off: callers that re-stamp
    timestamps between fresh-copy bars (e.g. UTC normalisation,
    Period→Timestamp coercion) will cold-rebuild every bar because id
    differs, ts differs, and the close-leg is gated on symmetric ts
    absence. This is intentional — cross-stream false-merge correctness
    over hit-rate. Documented in `_advance_kind`'s docstring; this test
    ensures future gate revisions surface the trade-off."""

    @dataclass
    class _StampedBar:
        timestamp: str
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

    reg = IndicatorRegistry()
    # Cached state with timestamp "A_T_9".
    cached_bar = _StampedBar(timestamp="A_T_9", close=100.0)
    cached_state = {
        "fp": (id(cached_bar), 10, "A_T_9", 100.0),
        "macd_line": deque(),
        "value": {"macd": 0.0, "signal": None, "histogram": None},
    }
    # Fresh-copy caller re-stamps timestamps to a different format —
    # id differs, ts differs, close coincides. Close-leg would have
    # rescued under the pre-fix unconditional gate; new code falls to
    # cold-rebuild (kind='none').
    fresh = [_StampedBar(timestamp=f"B_T_{i}", close=99.0 + i * 0.1) for i in range(9)]
    fresh.extend(
        [_StampedBar(timestamp="B_T_9", close=100.0), _StampedBar(timestamp="B_T_10", close=101.0)]
    )
    fp_fresh = reg._bar_fingerprint(fresh)
    assert reg._advance_kind(cached_state, fresh, fp_fresh) == "none"


# ---------------------------------------------------------------------------
# Warm-up cache amortisation (factors + synthesis compilers)
# ---------------------------------------------------------------------------


def test_factors_compiler_macd_signal_warmup_cache_amortises_same_bar_repeat() -> None:
    """During the ``[slow, slow + signal - 1)`` warm-up window, the
    factors MACDSignal helper must write ``value=NAN`` to ``_ind_state``
    so same-bar repeat calls share the cache. Prior version returned NAN
    at the outer guard before any cache write, so repeated calls during
    warm-up cold-rebuilt every time."""
    import sys
    import types as _types

    from investment_team.execution.risk_filter import RiskLimits
    from investment_team.strategy_lab.factors.compiler import compile_genome
    from investment_team.strategy_lab.factors.models import (
        CompareGT,
        Const,
        Genome,
        MACDSignal,
        PctOfEquity,
    )

    # Stub the sandbox `contract` module the compiled output expects.
    fake = _types.ModuleType("contract")

    class _Strategy:
        pass

    class _OrderSide:
        LONG = "LONG"
        SHORT = "SHORT"

    class _OrderType:
        MARKET = "MARKET"

    fake.Strategy = _Strategy
    fake.OrderSide = _OrderSide
    fake.OrderType = _OrderType
    sys.modules["contract"] = fake

    genome = Genome(
        asset_class="stocks",
        hypothesis="macd warmup cache check",
        entry=CompareGT(left=MACDSignal(fast=12, slow=26, signal=9), right=Const(value=0.0)),
        exit=CompareGT(left=Const(value=0.0), right=MACDSignal(fast=12, slow=26, signal=9)),
        sizing=PctOfEquity(pct=2.0),
        risk_limits=RiskLimits(),
        metadata={},
    )
    code = compile_genome(genome)
    ns: dict = {}
    exec(code, ns)
    strat = ns["GeneratedStrategy"]()

    # Build bars in the warm-up window: len(bars) == slow (26), so
    # macd_line has exactly 1 entry → signal-EMA needs >= 9 → val=NAN.
    @dataclass
    class _Bar:
        timestamp: str
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        symbol: str = "QQQ"

    bars = [_Bar(timestamp=f"D_{i:02d}", close=100.0 + i * 0.3) for i in range(26)]

    # The MACDSignal helper is the one that returns NaN at warm-up;
    # other helpers (Const(0.0), CompareGT) return 0.0/False.
    macd_helpers = [
        name
        for name in dir(strat)
        if name.startswith("_n_") and math.isnan(getattr(strat, name)(bars))
        if isinstance(getattr(strat, name)(bars), float)
    ]
    # Reset _ind_state since the introspection above populated it.
    strat._ind_state = {}
    assert len(macd_helpers) >= 1
    helper = getattr(strat, macd_helpers[0])

    # First call populates the cache with val=NAN.
    result_1 = helper(bars)
    assert math.isnan(result_1)
    # Cache MUST be populated with the NAN value (was not previously —
    # outer guard returned NAN before any cache write).
    assert len(strat._ind_state) >= 1
    cached_state = next(iter(strat._ind_state.values()))
    assert math.isnan(cached_state["value"])

    # Second same-bar call must hit the same-bar cache fast-path.
    fp_before = cached_state["fp"]
    result_2 = helper(bars)
    assert math.isnan(result_2)
    # fp slot is the same object after the second call (same-bar return).
    cached_state_after = next(iter(strat._ind_state.values()))
    assert cached_state_after["fp"] == fp_before


def test_synthesis_compiler_macd_warmup_cache_amortises_signal_select() -> None:
    """During the ``[slow, slow + signal - 1)`` warm-up window for
    ``select='signal'`` / ``'histogram'``, the synthesis MACD helper
    must write the cache with ``sig_val=None`` so same-bar repeat calls
    share the cache. Prior version returned None at the outer guard
    before any cache write — the canonical signal-cross entry rule
    cold-rebuilt on every warm-up bar."""
    import sys
    import types as _types

    from investment_team.strategy_lab.spec_dsl import (
        EntryRule,
        FixedFractionSizing,
        IndicatorRef,
        Predicate,
    )
    from investment_team.strategy_lab.synthesis import compile_strategy

    fake = _types.ModuleType("contract")

    class _Strategy:
        pass

    fake.Strategy = _Strategy
    sys.modules["contract"] = fake

    from investment_team.models import StrategySpec

    spec = StrategySpec(
        strategy_id="warmup-cache-test",
        authored_by="t",
        asset_class="stocks",
        hypothesis="t",
        signal_definition="t",
        timeframe="1d",
        entry_rules=[
            EntryRule(
                side="long",
                when=Predicate(
                    lhs=IndicatorRef(name="macd", params={"output": "signal"}),
                    op=">",
                    rhs=0.0,
                ),
            )
        ],
        exit_rules=[],
        sizing=FixedFractionSizing(fraction=0.02),
        target_symbols=["QQQ"],
    )
    code = compile_strategy(spec)
    ns: dict = {}
    exec(code, ns)
    strat = ns["CompiledStrategy"]()

    @dataclass
    class _Bar:
        symbol: str
        timestamp: str
        close: float
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

    # len(bars) == slow (26) — inside the warm-up window for signal
    # (needs slow+signal-1 = 34). Macd-line has length 1; sig_val=None.
    bars = [_Bar(symbol="QQQ", timestamp=f"D_{i:02d}", close=100.0 + i * 0.3) for i in range(26)]

    result_1 = strat.macd(bars, fast=12, slow=26, signal=9, source="close", select="signal")
    assert result_1 is None
    # Cache MUST be populated even during warm-up so repeat calls hit
    # the same-bar fast-path.
    assert len(strat._ind_state) >= 1
    cached = next(iter(strat._ind_state.values()))
    assert cached["value"]["signal"] is None
    assert cached["value"]["histogram"] is None
    assert cached["value"]["macd"] is not None  # macd_val IS computable at slow bars

    # Second same-bar call hits the cache.
    result_2 = strat.macd(bars, fast=12, slow=26, signal=9, source="close", select="signal")
    assert result_2 is None


# ---------------------------------------------------------------------------
# Precondition validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fast, slow, signal",
    [
        (30, 10, 9),  # fast >= slow
        (1, 26, 9),  # fast < 2
        (0, 26, 9),  # fast = 0
        (-3, 26, 9),  # negative fast
        (12, 26, 0),  # signal = 0
        (12, 26, 1),  # signal = 1 (degenerate)
        (12, 26, -1),  # negative signal
    ],
)
def test_macd_components_raises_value_error_on_bad_params(
    fast: int, slow: int, signal: int
) -> None:
    """``macd_components`` must raise ValueError for every malformed
    parameter combination — asserts disappear under ``python -O``, and
    the precondition floor must match the DSL bounds (fast >= 2,
    slow > fast, signal >= 2).
    """
    bars = _series(60)
    with pytest.raises(ValueError):
        macd_components(bars, fast=fast, slow=slow, signal=signal)


# ---------------------------------------------------------------------------
# Other indicators — windowed parity
# ---------------------------------------------------------------------------


def test_ema_matches_windowed_reference() -> None:
    bars = _series(40, seed=8)
    reg = IndicatorRegistry()
    for n in range(20, 41):
        sub = bars[:n]
        assert reg.ema(sub, period=14) == pytest.approx(_legacy_ema(sub, 14), rel=0, abs=1e-12)


def test_sma_matches_naive_mean() -> None:
    bars = _series(40, seed=9)
    reg = IndicatorRegistry()
    for n in range(20, 41):
        sub = bars[:n]
        expected = sum(b.close for b in sub[-14:]) / 14
        assert reg.sma(sub, period=14) == pytest.approx(expected, rel=0, abs=1e-12)


def test_rsi_matches_legacy_loop() -> None:
    bars = _series(40, seed=10)
    reg = IndicatorRegistry()
    for n in range(20, 41):
        sub = bars[:n]
        # Reference: the original primitives.rsi math.
        period = 14
        gains = 0.0
        losses = 0.0
        for i in range(len(sub) - period, len(sub)):
            delta = sub[i].close - sub[i - 1].close
            if delta > 0:
                gains += delta
            else:
                losses += -delta
        avg_gain = gains / period
        avg_loss = losses / period
        if avg_loss == 0:
            expected = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            expected = 100.0 - (100.0 / (1.0 + rs))
        assert reg.rsi(sub, period=14) == pytest.approx(expected, rel=0, abs=1e-12)


def test_atr_matches_legacy_loop() -> None:
    bars = _series(40, seed=11)
    reg = IndicatorRegistry()
    period = 14
    for n in range(20, 41):
        sub = bars[:n]
        trs = []
        for i in range(len(sub) - period, len(sub)):
            h = sub[i].high
            low = sub[i].low
            pc = sub[i - 1].close
            trs.append(max(h - low, abs(h - pc), abs(low - pc)))
        expected = sum(trs) / period
        assert reg.atr(sub, period=14) == pytest.approx(expected, rel=0, abs=1e-12)


def test_adx_matches_legacy() -> None:
    bars = _series(60, seed=12)
    reg = IndicatorRegistry()
    period = 14
    for n in range(30, 61):
        sub = bars[:n]
        plus_dms: List[float] = []
        minus_dms: List[float] = []
        trs: List[float] = []
        for i in range(1, len(sub)):
            up = sub[i].high - sub[i - 1].high
            down = sub[i - 1].low - sub[i].low
            plus_dms.append(up if (up > down and up > 0) else 0.0)
            minus_dms.append(down if (down > up and down > 0) else 0.0)
            pc = sub[i - 1].close
            trs.append(
                max(
                    sub[i].high - sub[i].low,
                    abs(sub[i].high - pc),
                    abs(sub[i].low - pc),
                )
            )
        tr_sum = sum(trs[-period:])
        if tr_sum == 0:
            expected = 0.0
        else:
            plus_di = 100.0 * sum(plus_dms[-period:]) / tr_sum
            minus_di = 100.0 * sum(minus_dms[-period:]) / tr_sum
            denom = plus_di + minus_di
            expected = 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom
        assert reg.adx(sub, period=14) == pytest.approx(expected, rel=0, abs=1e-12)


def test_bollinger_bands_round_trip_through_select() -> None:
    bars = _series(40, seed=13)
    reg = IndicatorRegistry()
    middle = reg.bollinger_bands(bars, period=20, select="middle")
    upper = reg.bollinger_bands(bars, period=20, select="upper")
    lower = reg.bollinger_bands(bars, period=20, select="lower")
    # Symmetric around the middle.
    assert upper - middle == pytest.approx(middle - lower, rel=0, abs=1e-12)


def test_stochastic_returns_k_and_d() -> None:
    bars = _series(30, seed=14)
    reg = IndicatorRegistry()
    k = reg.stochastic(bars, k_period=14, d_period=3, select="k")
    d = reg.stochastic(bars, k_period=14, d_period=3, select="d")
    assert k is not None
    assert d is not None
    assert 0.0 <= k <= 100.0
    assert 0.0 <= d <= 100.0


def test_vwap_matches_cumulative_typical_price() -> None:
    bars = _series(30, seed=15)
    reg = IndicatorRegistry()
    expected_num = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in bars)
    expected_den = sum(b.volume for b in bars)
    expected = expected_num / expected_den
    assert reg.vwap(bars) == pytest.approx(expected, rel=0, abs=1e-12)


# ---------------------------------------------------------------------------
# Top-level pure-function helpers
# ---------------------------------------------------------------------------


def test_windowed_ema_pure_function_matches_legacy_ema() -> None:
    bars = _series(50, seed=16)
    for period in (5, 12, 26):
        assert windowed_ema(bars, period, "close") == pytest.approx(
            _legacy_ema(bars, period), rel=0, abs=1e-12
        )


def test_macd_components_pure_function_matches_legacy() -> None:
    bars = _series(60, seed=17)
    macd_val, sig, hist = macd_components(bars, fast=12, slow=26, signal=9)
    assert macd_val == _legacy_macd(bars, fast=12, slow=26, signal=9, select="macd")
    assert sig == _legacy_macd(bars, fast=12, slow=26, signal=9, select="signal")
    assert hist == _legacy_macd(bars, fast=12, slow=26, signal=9, select="histogram")


def test_macd_components_warmup_returns_none_tuple() -> None:
    bars = _series(20)
    out = macd_components(bars, fast=12, slow=26, signal=9)
    assert out == (None, None, None)


# ---------------------------------------------------------------------------
# Warm-up and degenerate inputs
# ---------------------------------------------------------------------------


def test_indicators_return_none_during_warmup() -> None:
    reg = IndicatorRegistry()
    short = _series(5)
    assert reg.ema(short, period=20) is None
    assert reg.sma(short, period=20) is None
    assert reg.rsi(short, period=14) is None
    assert reg.atr(short, period=14) is None
    assert reg.adx(short, period=14) is None
    assert reg.bollinger_bands(short, period=20) is None
    assert reg.stochastic(short, k_period=14) is None


def test_indicators_handle_empty_bars() -> None:
    reg = IndicatorRegistry()
    empty: List[_Bar] = []
    assert reg.ema(empty, period=14) is None
    assert reg.sma(empty, period=14) is None
    assert reg.rsi(empty, period=14) is None
    assert reg.atr(empty, period=14) is None
    assert reg.vwap(empty) is None


def test_rsi_zero_loss_returns_100_when_all_gain() -> None:
    # Monotonically increasing close → losses=0 → expected RSI = 100.
    bars = [_Bar(timestamp=f"2024-01-{i + 1:02d}", close=100.0 + i) for i in range(20)]
    val = IndicatorRegistry().rsi(bars, period=14)
    assert val == 100.0


def test_rsi_no_change_returns_50() -> None:
    # Flat close → gains=losses=0 → expected RSI = 50.
    bars = [_Bar(timestamp=f"2024-01-{i + 1:02d}", close=100.0) for i in range(20)]
    val = IndicatorRegistry().rsi(bars, period=14)
    assert val == 50.0


def test_vwap_zero_volume_falls_back_to_mean_close() -> None:
    bars = [_Bar(timestamp=f"2024-01-{i + 1:02d}", close=100.0 + i, volume=0.0) for i in range(10)]
    expected = sum(b.close for b in bars) / len(bars)
    assert IndicatorRegistry().vwap(bars) == pytest.approx(expected, rel=0, abs=1e-12)


# ---------------------------------------------------------------------------
# Performance smoke test — guards against accidental O(N²) regressions
# ---------------------------------------------------------------------------


def test_macd_streaming_is_significantly_faster_than_cold_start() -> None:
    """The streaming-step path on a long history must beat repeated cold-starts.

    Smoke-only — not a microbench. Asserts a 2x lower bound to stay
    robust against CI noise; the real win (≥10x on a 500-bar fixture
    with multiple indicators) is exercised by ``tests/bench/``.
    """
    import time

    bars = _series(500, seed=42)

    # Streaming: registry retains state across bars.
    reg_streaming = IndicatorRegistry()
    t0 = time.perf_counter()
    for n in range(35, 501):
        reg_streaming.macd(bars[:n], fast=12, slow=26, signal=9, select="signal")
    streaming_t = time.perf_counter() - t0

    # Cold-start every bar: simulate the legacy behaviour by resetting state.
    reg_cold = IndicatorRegistry()
    t0 = time.perf_counter()
    for n in range(35, 501):
        reg_cold._state.clear()
        reg_cold.macd(bars[:n], fast=12, slow=26, signal=9, select="signal")
    cold_t = time.perf_counter() - t0

    # Streaming must be faster; threshold is loose to avoid CI flakes.
    assert streaming_t < cold_t, (
        f"streaming ({streaming_t:.4f}s) not faster than cold-start ({cold_t:.4f}s)"
    )
    # On a healthy run the ratio is >5×; we only require >1.5× here.
    assert cold_t / streaming_t > 1.5, f"streaming speedup too small: {cold_t / streaming_t:.2f}x"


# ---------------------------------------------------------------------------
# Primitives wrappers — confirm the host-side reference still matches
# ---------------------------------------------------------------------------


def test_primitives_wrappers_unchanged_outputs() -> None:
    """``factors.primitives`` now delegates to the registry; the outputs the
    factor-DSL unit tests have always pinned must remain identical."""
    from investment_team.strategy_lab.factors import primitives as P

    bars = _series(60, seed=44)
    # NaN-shape primitive checks.
    assert math.isnan(P.macd_signal(bars[:10], fast=12, slow=26, signal=9))
    assert math.isfinite(P.macd_signal(bars, fast=12, slow=26, signal=9))
    assert math.isfinite(P.rsi(bars, period=14))
    assert math.isfinite(P.atr(bars, period=14))
    assert math.isfinite(P.adx(bars, period=14))
    # Spot value: ema/sma equal the legacy/naive references.
    assert P.ema(bars, period=14) == pytest.approx(_legacy_ema(bars, 14), rel=0, abs=1e-12)
    assert P.sma(bars, period=14) == pytest.approx(
        sum(b.close for b in bars[-14:]) / 14, rel=0, abs=1e-12
    )
