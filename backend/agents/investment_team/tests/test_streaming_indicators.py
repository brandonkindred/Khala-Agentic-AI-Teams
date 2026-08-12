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


def test_macd_expand_signal_histogram_reads_stay_o1_amortized(monkeypatch) -> None:
    """O(1)-amortized proof for MACD ``signal``/``histogram`` reads on the
    ``expand`` path — sibling indicators (OBV, MFI, Bollinger) prove their
    O(1)-amortized cost structurally, via a bounded running-sum/deque that
    never grows with bars seen. MACD's incremental fix has no single
    bounded buffer to inspect from outside, so this proof instruments both
    halves of the recurrence instead:

    * the fast/slow EMA legs — every ``windowed_ema`` call must operate on
      a fixed-size window, never one that grows with history;
    * the signal-line EMA itself — a full ``iter(macd_line)`` walk (the
      pre-fix behaviour) must fire only once, at the one-time warm-up
      crossing. Checking only the EMA-leg calls would miss a regression
      here: if the incremental single-step ever fell back to re-walking
      ``macd_line`` on every bar, ``windowed_ema`` call counts would be
      completely unaffected (that walk touches the cached deque, not the
      fast/slow legs), so both must be pinned for this test to actually
      enforce the O(1)-amortized guarantee end to end.

    Distinct from the existing ``bench/`` timing tests (wall-clock ratios,
    opt-in via ``-m bench``): this is deterministic, always runs in CI, and
    also proves the second per-bar ``select`` read is a free same-bar cache
    hit rather than doubling the call count.
    """
    import investment_team.strategy_lab.indicators.streaming as streaming

    fast, slow, signal = 12, 26, 9
    bars = _series(600, seed=91)

    call_lengths: List[int] = []
    original_windowed_ema = streaming.windowed_ema

    def _ema_spy(bar_window, period, source="close"):
        call_lengths.append(len(bar_window))
        return original_windowed_ema(bar_window, period, source)

    monkeypatch.setattr(streaming, "windowed_ema", _ema_spy)

    signal_walk_calls = 0
    real_deque = streaming.deque

    class _CountingDeque(real_deque):
        def __iter__(self):
            nonlocal signal_walk_calls
            signal_walk_calls += 1
            return super().__iter__()

    monkeypatch.setattr(streaming, "deque", _CountingDeque)

    reg = IndicatorRegistry()
    for n in range(slow, len(bars) + 1):
        sub = bars[:n]
        reg.macd(sub, fast=fast, slow=slow, signal=signal, select="signal")
        reg.macd(sub, fast=fast, slow=slow, signal=signal, select="histogram")

    # Every windowed_ema call operates on a fixed-size window (fast or
    # slow) — never one that scales with `n`. A regression to a per-bar
    # full re-walk (the legacy O(N·W) shape) would show lengths climbing
    # toward `len(bars)`.
    assert call_lengths, "expected at least one windowed_ema call"
    assert set(call_lengths) <= {fast, slow}

    # Exactly two calls per streamed bar (fast + slow leg) — the second
    # `select="histogram"` read on the same bar is a same-bar cache hit
    # and adds zero extra calls, so the total stays linear in bar count
    # instead of doubling.
    expected_calls = 2 * (len(bars) - slow + 1)
    assert len(call_lengths) == expected_calls, (
        f"expected {expected_calls} windowed_ema calls, got {len(call_lengths)} "
        "— extra calls indicate a hidden re-walk or lost same-bar caching"
    )

    # The full macd_line walk fires exactly once — the warm-up bar where
    # the signal EMA first has enough history to fill. Every later expand
    # step must single-step from the cached signal value instead.
    assert signal_walk_calls == 1, (
        f"expected exactly 1 full macd_line walk (the warm-up crossing), got "
        f"{signal_walk_calls} — the signal-EMA recurrence is re-walking the "
        "deque instead of single-stepping from the cached value"
    )


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
# MACD — golden baseline (characterization; pins current pre-fix output)
#
# Unlike the parity tests above (which check the registry against an
# independently-written reference implementation, ``_legacy_macd``), these
# tests pin literal numeric constants captured from the CURRENT
# ``_macd_value`` implementation — the one that still re-walks the full
# ``macd_line`` deque to recompute the signal-line EMA on every call. They
# are the golden baseline the incremental O(1) rewrite (a sibling issue)
# must reproduce bit-for-bit; unlike the parity tests, this baseline can't
# silently drift if ``_legacy_macd`` itself is ever edited.
# ---------------------------------------------------------------------------


def test_macd_golden_baseline_expand_transitions() -> None:
    """Pins current output across the ``expand`` path: a single registry
    driven bar-by-bar (n=1..45) over a fixed seeded series."""
    bars = _series(45, seed=601)
    reg = IndicatorRegistry()
    golden = {
        26: (1.3175242222977062, None, None),
        30: (1.5093556546607942, None, None),
        35: (1.7489476141703335, 1.4830941545803946, 0.2658534595899389),
        40: (2.3228769295836713, 1.746521551146327, 0.5763553784373443),
        45: (2.7827410071213734, 2.134441625873138, 0.6482993812482354),
    }
    for n in range(1, len(bars) + 1):
        sub = bars[:n]
        if n == 40:
            # Confirm the transition under test is actually "expand" for a
            # representative later step, not implied only by loop shape.
            state = reg._peek(("macd", None, 12, 26, 9, "close"))
            fp = reg._bar_fingerprint(sub)
            assert reg._advance_kind(state, sub, fp) == "expand"
        m = reg.macd(sub, fast=12, slow=26, signal=9, select="macd")
        s = reg.macd(sub, fast=12, slow=26, signal=9, select="signal")
        h = reg.macd(sub, fast=12, slow=26, signal=9, select="histogram")
        if n in golden:
            exp_m, exp_s, exp_h = golden[n]
            assert m == pytest.approx(exp_m, rel=0, abs=1e-12), f"n={n} macd={m!r}"
            if exp_s is None:
                assert s is None and h is None, f"n={n} s={s!r} h={h!r}"
            else:
                assert s == pytest.approx(exp_s, rel=0, abs=1e-12), f"n={n} signal={s!r}"
                assert h == pytest.approx(exp_h, rel=0, abs=1e-12), f"n={n} histogram={h!r}"


def test_macd_golden_baseline_slide_transitions() -> None:
    """Pins current output across the ``slide`` path: a single registry
    driven over a fixed 40-bar sliding window across a longer series."""
    bars = _series(120, seed=602)
    window = 40
    reg = IndicatorRegistry()
    golden = {
        0: (2.567567144315447, 2.071800426047202, 0.49576671826824503),
        5: (2.3414399804443633, 2.292338075899163, 0.04910190454520036),
        15: (1.8356837115187261, 2.001669642130804, -0.16598593061207767),
        30: (1.5192284347039617, 1.8180268563542354, -0.2987984216502737),
        60: (2.2320794091741902, 1.806486677154389, 0.4255927320198012),
    }
    for offset in range(0, len(bars) - window + 1):
        sliding = bars[offset : offset + window]
        if offset == 5:
            state = reg._peek(("macd", None, 12, 26, 9, "close"))
            fp = reg._bar_fingerprint(sliding)
            assert reg._advance_kind(state, sliding, fp) == "slide"
        m = reg.macd(sliding, fast=12, slow=26, signal=9, select="macd")
        s = reg.macd(sliding, fast=12, slow=26, signal=9, select="signal")
        h = reg.macd(sliding, fast=12, slow=26, signal=9, select="histogram")
        if offset in golden:
            exp_m, exp_s, exp_h = golden[offset]
            assert m == pytest.approx(exp_m, rel=0, abs=1e-12), f"offset={offset} macd={m!r}"
            assert s == pytest.approx(exp_s, rel=0, abs=1e-12), f"offset={offset} signal={s!r}"
            assert h == pytest.approx(exp_h, rel=0, abs=1e-12), f"offset={offset} histogram={h!r}"


def test_macd_golden_baseline_reset_transitions() -> None:
    """Pins current output across the ``"none"``/reset path, in two forms:
    a fresh registry's first-ever (cold) call, and a warm registry forced
    backwards to an earlier bar count (a genuine mid-life reset, distinct
    from an initial cold-start on an empty cache)."""
    # -- Independent fresh-registry cold-starts. --
    fresh_bars = _series(60, seed=603)
    fresh_golden = {
        26: (1.9259170563526453, None, None),
        34: (1.1711831697481472, 1.910603117224805, -0.7394199474766578),
        40: (1.0703895180889305, 1.5796168062440215, -0.509227288155091),
        50: (1.770269234436853, 1.6169731655310817, 0.1532960689057712),
        60: (2.006039890130964, 2.0338790966289295, -0.027839206497965563),
    }
    for n, (exp_m, exp_s, exp_h) in fresh_golden.items():
        reg = IndicatorRegistry()
        sub = fresh_bars[:n]
        m = reg.macd(sub, fast=12, slow=26, signal=9, select="macd")
        s = reg.macd(sub, fast=12, slow=26, signal=9, select="signal")
        h = reg.macd(sub, fast=12, slow=26, signal=9, select="histogram")
        assert m == pytest.approx(exp_m, rel=0, abs=1e-12), f"n={n} macd={m!r}"
        if exp_s is None:
            assert s is None and h is None, f"n={n} s={s!r} h={h!r}"
        else:
            assert s == pytest.approx(exp_s, rel=0, abs=1e-12), f"n={n} signal={s!r}"
            assert h == pytest.approx(exp_h, rel=0, abs=1e-12), f"n={n} histogram={h!r}"

    # -- Mid-life reset: drive a registry forward, then jump it backwards. --
    warm_bars = _series(60, seed=604)
    reg = IndicatorRegistry()
    for n in range(26, 61):
        reg.macd(warm_bars[:n], fast=12, slow=26, signal=9, select="signal")
    truncated = warm_bars[:45]
    state = reg._peek(("macd", None, 12, 26, 9, "close"))
    fp = reg._bar_fingerprint(truncated)
    assert reg._advance_kind(state, truncated, fp) == "none"
    m = reg.macd(truncated, fast=12, slow=26, signal=9, select="macd")
    s = reg.macd(truncated, fast=12, slow=26, signal=9, select="signal")
    h = reg.macd(truncated, fast=12, slow=26, signal=9, select="histogram")
    assert m == pytest.approx(2.3299346994342613, rel=0, abs=1e-12)
    assert s == pytest.approx(1.8677057609652556, rel=0, abs=1e-12)
    assert h == pytest.approx(0.4622289384690057, rel=0, abs=1e-12)


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
    # numpy.ma.bool_ (submodule numpy.ma.core) — previously slipped past
    # the namespace gate that hardcoded ``__module__ in ('numpy','pandas')``.
    # The expanded ``__module__.split('.')[0]`` check catches it.
    assert reg._bar_fingerprint([_Bar(close=np.ma.bool_(True))])[3] is None
    assert reg._bar_fingerprint([_Bar(close=np.ma.bool_(False))])[3] is None


def test_bar_fingerprint_rejects_overflow_close() -> None:
    """An astronomical-magnitude int (synthetic upstream that produces
    ``close = 10**400`` after a bad multiply/divide) raises
    ``OverflowError`` from ``float()``. The normaliser must catch it
    alongside ``TypeError``/``ValueError`` and degrade to ``None`` —
    the docstring promises 'any value float() refuses → None'."""

    @dataclass
    class _Bar:
        close: object
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        timestamp: str = "T"

    reg = IndicatorRegistry()
    # int too large for float — OverflowError from float().
    huge = 10**400
    assert reg._bar_fingerprint([_Bar(close=huge)])[3] is None
    assert reg._bar_fingerprint([_Bar(close=-huge)])[3] is None


def test_bar_fingerprint_handles_property_close_that_raises() -> None:
    """``getattr(bar, 'close', None)`` only short-circuits when the
    attribute name doesn't resolve — exceptions raised INSIDE a
    ``@property`` body bubble up. The new ``_safe_read_close`` wrapper
    catches them so the cache layer remains exception-safe."""

    class _RaisingBar:
        timestamp = "T"
        open = 100.0
        high = 100.0
        low = 100.0
        volume = 1.0

        @property
        def close(self):
            raise RuntimeError("close not loaded yet")

    reg = IndicatorRegistry()
    fp = reg._bar_fingerprint([_RaisingBar()])
    assert fp[3] is None  # close slot defensively None, no crash.


def test_bar_fingerprint_handles_pyarrow_boolean_scalars() -> None:
    """PyArrow boolean scalars live outside the original (numpy, pandas)
    hardcoded set. The expanded module gate accepts the ``pyarrow``
    top-level package."""
    pa = pytest.importorskip("pyarrow")

    @dataclass
    class _Bar:
        close: object
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        timestamp: str = "T"

    reg = IndicatorRegistry()
    # PyArrow BooleanScalar lives in pyarrow.lib (top-level: pyarrow).
    assert reg._bar_fingerprint([_Bar(close=pa.scalar(True))])[3] is None
    assert reg._bar_fingerprint([_Bar(close=pa.scalar(False))])[3] is None


def test_bar_fingerprint_handles_polars_boolean_scalars() -> None:
    """Polars boolean scalars are normally Python ``bool`` (caught by
    the ``isinstance(raw, bool)`` gate), but synthetic types whose
    ``__module__`` is ``polars*`` and name is a canonical bool variant
    must also be rejected via the third-party module gate. Pins
    coverage of the ``polars`` branch of the allowlist that the
    pyarrow test does not exercise."""

    class _PolarsBoolean:
        """Synthetic stand-in for a polars boolean scalar — pinning the
        allowlist contract without requiring polars as a test dep."""

        __module__ = "polars.internals.scalar"
        # __name__ is set on the class object; type(_x).__name__ is the
        # class's __name__ attribute. Default would be '_PolarsBoolean'.

        def __float__(self) -> float:
            return 1.0  # Without the gate this would silently coerce.

    _PolarsBoolean.__name__ = "Boolean"

    @dataclass
    class _Bar:
        close: object
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        timestamp: str = "T"

    reg = IndicatorRegistry()
    # The third-party gate matches by (top-level module, exact-name allowlist);
    # rejected ⇒ close-slot is None rather than the would-be float() value.
    fp = reg._bar_fingerprint([_Bar(close=_PolarsBoolean())])
    assert fp[3] is None, "polars-Boolean stand-in must be rejected by the third-party gate"


def test_bar_fingerprint_substring_match_does_not_overreach() -> None:
    """The new exact-name allowlist must NOT false-positive on type
    names that merely contain 'bool' as a substring (e.g. a hypothetical
    'BoolWrapper' or 'BooleanIndex'). Only canonical boolean scalar
    type names should be rejected."""

    # Build a synthetic class in module 'numpy' whose name contains
    # 'bool' but is NOT a real boolean — e.g. a notional dtype wrapper.
    class BooleanIndex:
        __module__ = "numpy"
        __name__ = "BooleanIndex"

        def __float__(self):
            return 42.0

    # Manually pin __module__ on the class object itself.
    BooleanIndex.__module__ = "numpy"

    @dataclass
    class _Bar:
        close: object
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        timestamp: str = "T"

    reg = IndicatorRegistry()
    # Old substring-based check would have rejected anything with 'bool'
    # in the name; the new exact-name allowlist passes BooleanIndex
    # through to float() (which returns 42.0 for this synthetic class).
    fp = reg._bar_fingerprint([_Bar(close=BooleanIndex())])
    assert fp[3] == 42.0


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
    # Hermetic id sentinels — use ``id()`` of throwaway objects so
    # ``id(fresh_a[-2]) == sentinel`` never accidentally aliases on
    # any CPython build. Pre-fix: hardcoded magic integers (12345,
    # 54321) could in principle collide with future allocations.
    _id_sentinel_a = id(object())
    _id_sentinel_b = id(object())

    # Case A: cached side has ts, current side does NOT — asymmetric.
    cached_state = {
        "fp": (_id_sentinel_a, 10, "T_9", 100.0),
        "macd_line": deque(),
        "value": {"macd": 0.0, "signal": None, "histogram": None},
    }
    fresh_a = [_NoTSBar(close=99.0 + i * 0.1) for i in range(9)]
    fresh_a.extend([_NoTSBar(close=100.0), _NoTSBar(close=101.0)])  # len=11
    # Verify the id sentinel doesn't accidentally alias the live bar's id.
    assert id(fresh_a[-2]) != _id_sentinel_a
    fp_a = reg._bar_fingerprint(fresh_a)
    # bars[-2] has close=100.0 matching cached fp[3], but ts is asymmetric.
    # MUST NOT classify as expand/slide — close-leg is gated on
    # symmetric ts absence.
    assert reg._advance_kind(cached_state, fresh_a, fp_a) == "none"

    # Case B: cached side has NO ts, current side does — also asymmetric.
    cached_state_no_ts = {
        "fp": (_id_sentinel_b, 10, None, 100.0),
        "macd_line": deque(),
        "value": {"macd": 0.0, "signal": None, "histogram": None},
    }
    fresh_b = [_TSBar(timestamp=f"U_{i}", close=99.0 + i * 0.1) for i in range(9)]
    fresh_b.extend([_TSBar(timestamp="U_9", close=100.0), _TSBar(timestamp="U_10", close=101.0)])
    assert id(fresh_b[-2]) != _id_sentinel_b
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


def test_factors_compiler_macd_signal_warmup_cache_amortises_same_bar_repeat(monkeypatch) -> None:
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
    # monkeypatch.setitem restores sys.modules["contract"] to whatever it
    # was (or removes it) at teardown, so this test can't leak state into
    # others that import the real module.
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
    monkeypatch.setitem(sys.modules, "contract", fake)

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
    # outer guard returned NAN before any cache write). ``value`` now holds
    # all three MACD outputs (macd/signal/histogram) — the shared
    # render_macd_body template computes them together and dispatches on
    # ``select`` — so the signal-line NaN this test checks lives under
    # ``value["signal"]``.
    assert len(strat._ind_state) >= 1
    cached_state = next(iter(strat._ind_state.values()))
    assert math.isnan(cached_state["value"]["signal"])

    # Second same-bar call must hit the same-bar fast-path. Pin this by
    # identity of the cached dict — a regression that re-cold-rebuilds
    # would produce a NEW dict object (cache write replaces it) with
    # equal contents. The `is` check fails loudly on rebuild even when
    # contents are structurally identical.
    macd_line_before = cached_state["macd_line"]
    result_2 = helper(bars)
    assert math.isnan(result_2)
    cached_state_after = next(iter(strat._ind_state.values()))
    assert cached_state_after is cached_state, (
        "same-bar fast-path must reuse the cached dict object — a rebuild "
        "would replace it via `self._ind_state[key] = {...}`"
    )
    assert cached_state_after["macd_line"] is macd_line_before, (
        "same-bar fast-path must not allocate a new macd_line deque"
    )


def test_synthesis_compiler_macd_warmup_cache_amortises_signal_select(monkeypatch) -> None:
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

    class _OrderSide:
        LONG = "LONG"
        SHORT = "SHORT"

    class _OrderType:
        MARKET = "MARKET"

    # ``OrderSide``/``OrderType`` aren't exercised here but the compiler
    # unconditionally emits ``from contract import OrderSide, OrderType,
    # Strategy``. monkeypatch.setitem restores sys.modules["contract"] to
    # whatever it was (or removes it) at teardown, so this stub can't leak
    # into other tests that import the real module.
    fake.Strategy = _Strategy
    fake.OrderSide = _OrderSide
    fake.OrderType = _OrderType
    monkeypatch.setitem(sys.modules, "contract", fake)

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

    # Second same-bar call hits the same-bar fast-path. Pin identity
    # of the cached dict and macd_line deque — a rebuild would replace
    # them with fresh structurally-equal objects.
    macd_line_before = cached["macd_line"]
    result_2 = strat.macd(bars, fast=12, slow=26, signal=9, source="close", select="signal")
    assert result_2 is None
    cached_after = next(iter(strat._ind_state.values()))
    assert cached_after is cached, "same-bar fast-path must reuse the cached dict (was rebuilt)"
    assert cached_after["macd_line"] is macd_line_before, (
        "same-bar fast-path must not allocate a new macd_line deque"
    )


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


@pytest.mark.parametrize(
    "fast, slow, signal",
    [
        (float("nan"), 26, 9),
        (12, float("nan"), 9),
        (12, 26, float("nan")),
    ],
)
def test_macd_components_rejects_nan_params(fast: float, slow: float, signal: float) -> None:
    """``NaN`` parameters are unordered with everything under IEEE 754;
    a strict ``signal < 2`` check evaluates False on NaN and silently
    admits it, after which ``alpha_g = 2.0 / (NaN + 1.0) = NaN`` poisons
    the macd_line. The ``not (x >= 2)`` rewrite must catch this so
    ``macd_components`` and the reference primitive ``macd_signal``
    behave consistently with ``IndicatorRegistry.macd`` (which raises).
    """
    bars = _series(60)
    with pytest.raises(ValueError):
        macd_components(bars, fast=fast, slow=slow, signal=signal)


def test_macd_value_rejects_non_int_params() -> None:
    """Float-valued parameters (e.g. ``fast=2.5``) pass the value gate
    (``2.5 >= 2`` is True) but then break ``bars[-fast:]`` with
    ``TypeError: slice indices must be integers``. The type gate
    rejects them upfront so the failure mode is the documented
    ``ValueError``, not a slicing crash."""
    bars = _series(60)
    reg = IndicatorRegistry()
    with pytest.raises(ValueError, match="integer"):
        reg.macd(bars, fast=2.5, slow=26, signal=9)
    with pytest.raises(ValueError, match="integer"):
        reg.macd(bars, fast=12, slow=26.0, signal=9)
    with pytest.raises(ValueError, match="integer"):
        reg.macd(bars, fast=12, slow=26, signal=9.5)


def test_normalise_close_survives_none_module() -> None:
    """``cls.__module__`` is normally a string but can be ``None`` for
    dynamically-built classes (``type()`` without a module) or exotic
    C-extension types. The lenient cache layer must NOT crash with
    ``AttributeError: 'NoneType' has no attribute 'split'`` — it must
    fall through to the ``float()`` path or degrade to ``None``."""

    class _Sneaky:
        __module__ = None  # type: ignore[assignment]

        def __float__(self) -> float:
            return 42.0

    @dataclass
    class _Bar:
        close: object
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0
        timestamp: str = "T"

    reg = IndicatorRegistry()
    fp = reg._bar_fingerprint([_Bar(close=_Sneaky())])
    # Must not crash; close-slot falls through to float() => 42.0.
    assert fp[3] == 42.0


def test_safe_getattr_propagates_programmer_signals() -> None:
    """The defensive attribute reader catches descriptor-resolution
    failures (``AttributeError``/``TypeError``/``ValueError``/
    ``RuntimeError``/``LookupError``) but MUST propagate programmer
    signals (``NotImplementedError``, ``AssertionError``) and runtime/
    interpreter signals (``MemoryError``, ``KeyboardInterrupt``). A
    blanket ``except Exception:`` would mask legitimate bugs."""
    from investment_team.strategy_lab.indicators.streaming import _safe_getattr

    class _RaiseAttr:
        @property
        def close(self) -> float:
            raise AttributeError("expected — must be caught")

    class _RaiseNotImpl:
        @property
        def close(self) -> float:
            raise NotImplementedError("subclass-override sentinel")

    class _RaiseAssert:
        @property
        def close(self) -> float:
            raise AssertionError("debug invariant")

    # Documented catches → degrade to None.
    assert _safe_getattr(_RaiseAttr(), "close") is None
    # Programmer signals → must propagate.
    with pytest.raises(NotImplementedError):
        _safe_getattr(_RaiseNotImpl(), "close")
    with pytest.raises(AssertionError):
        _safe_getattr(_RaiseAssert(), "close")


def test_normalise_close_canonical_helper_and_inlined_mirrors_stay_in_sync() -> None:
    """``_normalise_close`` lives once in indicators/streaming.py; the emitted
    MACD helper text (inlined — the sandbox import whitelist forbids the
    emitted code from importing the host helper) derives from exactly one
    authored copy in ``indicators/template_bodies.py`` (a local ``_norm_close``
    helper the emitted method defines once and calls for both the current and
    previous bar), shared by both DSL compilers via ``render_macd_body``.

    This meta-test pins the load-bearing token sets — third-party module
    allowlist, exact-name allowlist, exception tuple — verbatim across the
    canonical registry helper and the one shared emitted-text template, and
    asserts neither compiler re-duplicates the text itself. A future
    contributor extending the canonical (e.g. adding ``cudf`` to the module
    gate, or ``MemoryError`` to the exception tuple) gets a failing test
    pointing at the single template needing the edit.
    """
    import importlib.resources as _res
    import re
    from pathlib import Path

    repo_root = Path(
        _res.files("investment_team").joinpath("..").resolve()  # type: ignore[attr-defined]
    )
    sites = {
        "registry": repo_root / "investment_team/strategy_lab/indicators/streaming.py",
        "template_bodies": repo_root / "investment_team/strategy_lab/indicators/template_bodies.py",
    }
    # Whitespace-collapse: black/ruff may split a tuple literal across
    # lines; collapse whitespace runs into a single space so the token
    # match is invariant under formatting changes. Also normalise
    # both quoting styles ('x' vs "x") to single-quoted before matching.

    def _normalise(text: str) -> str:
        # Convert "xyz" → 'xyz' so quoting style doesn't matter.
        out = re.sub(r'"([^"\\]*)"', r"'\1'", text)
        # Collapse runs of whitespace to a single space.
        out = re.sub(r"\s+", " ", out)
        # Strip whitespace adjacent to parens and remove trailing commas
        # before a closing paren so multi-line tuple literals (formatted
        # by ruff/black) normalise to the same shape as inline tuples.
        out = re.sub(r"\(\s+", "(", out)
        out = re.sub(r",\s*\)", ")", out)
        return out

    required_tokens = [
        # Module allowlist — exact membership.
        "('numpy', 'pandas', 'pyarrow', 'polars')",
        # Exact-name allowlist (lower-cased forms accepted by the gate).
        "('bool', 'bool_', 'boolean', 'booleanscalar', 'boolscalar', 'bool8')",
        # Exception tuple inside the float() try/except.
        "(TypeError, ValueError, OverflowError)",
    ]
    for name, path in sites.items():
        src = _normalise(path.read_text(encoding="utf-8"))
        # Both sites now carry exactly one canonical copy: the registry's
        # ``_normalise_close`` function, and template_bodies' ``_norm_close``
        # local helper (called for both the current and previous bar).
        min_count = 1
        for tok in required_tokens:
            count = src.count(tok)
            assert count >= min_count, (
                f"site {name!r} missing canonical token {tok!r}; "
                f"expected >= {min_count}, got {count} — "
                "did you update the canonical _normalise_close without "
                "updating every inlined mirror?"
            )
        # Structural check: every site must guard ``__module__ is None``
        # via the ``isinstance(_mod, str)`` shape (matches the canonical).
        assert "__module__" in src and "isinstance" in src, (
            f"site {name!r} missing the __module__ None-guard pattern; "
            "see _normalise_close at indicators/streaming.py"
        )

    # Neither compiler may re-duplicate the inlined text itself — both must
    # render it from the one shared template.
    for compiler_path in (
        repo_root / "investment_team/strategy_lab/synthesis/compiler.py",
        repo_root / "investment_team/strategy_lab/factors/compiler.py",
    ):
        compiler_src = compiler_path.read_text(encoding="utf-8")
        assert "render_macd_body" in compiler_src, (
            f"{compiler_path} must render its MACD helper via "
            "indicators.template_bodies.render_macd_body, not an inlined mirror"
        )
        for tok in required_tokens:
            assert tok not in _normalise(compiler_src), (
                f"{compiler_path} re-duplicates canonical token {tok!r} inline; "
                "route it through indicators.template_bodies.render_macd_body instead"
            )


def test_bar_fingerprint_handles_raising_timestamp_property() -> None:
    """The asymmetry where only ``.close`` was wrapped meant a raising
    ``@property timestamp`` crashed ``_bar_fingerprint`` before
    ``_safe_read_close`` was reached. After generalising the defense
    via ``_safe_getattr``, a raising timestamp degrades to ``ts=None``
    just like a raising close degrades to ``close=None``."""

    @dataclass
    class _Bar:
        close: float = 100.0
        open: float = 100.0
        high: float = 100.0
        low: float = 100.0
        volume: float = 1.0

        @property
        def timestamp(self) -> str:
            raise RuntimeError("lazy timestamp not loaded")

    reg = IndicatorRegistry()
    fp = reg._bar_fingerprint([_Bar()])
    assert fp[2] is None
    assert fp[3] == 100.0  # close still resolves normally


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


def _legacy_adx(bars: List[_Bar], period: int) -> float:
    """Cold reference: rebuild every DM/TR triple from bar 1 (legacy form)."""
    plus_dms: List[float] = []
    minus_dms: List[float] = []
    trs: List[float] = []
    for i in range(1, len(bars)):
        up = bars[i].high - bars[i - 1].high
        down = bars[i - 1].low - bars[i].low
        plus_dms.append(up if (up > down and up > 0) else 0.0)
        minus_dms.append(down if (down > up and down > 0) else 0.0)
        pc = bars[i - 1].close
        trs.append(max(bars[i].high - bars[i].low, abs(bars[i].high - pc), abs(bars[i].low - pc)))
    tr_sum = sum(trs[-period:])
    if tr_sum == 0:
        return 0.0
    plus_di = 100.0 * sum(plus_dms[-period:]) / tr_sum
    minus_di = 100.0 * sum(minus_dms[-period:]) / tr_sum
    denom = plus_di + minus_di
    return 0.0 if denom == 0 else 100.0 * abs(plus_di - minus_di) / denom


def test_adx_sliding_window_matches_legacy() -> None:
    """A registry driven with a fixed-length sliding window must match a cold
    rebuild on each slide — the bounded DM/TR deque is trimmed correctly.

    The expand path is already covered by ``test_adx_matches_legacy`` (growing
    prefix). This pins the slide path that the incremental deque introduced."""
    bars = _series(120, seed=91)
    window_size = 50  # > 2*period + 1 so ADX is computable from the slice
    reg = IndicatorRegistry()
    for offset in range(0, len(bars) - window_size + 1):
        sliding = bars[offset : offset + window_size]
        streaming = reg.adx(sliding, period=14)
        cold = _legacy_adx(sliding, 14)
        assert streaming == pytest.approx(cold, rel=0, abs=1e-12), (
            f"offset={offset} streaming={streaming!r} cold={cold!r}"
        )


def test_adx_replay_falls_back_to_cold_start() -> None:
    """Feeding the registry a shorter history forces a cold-start fallback
    rather than carrying forward the longer history's DM/TR deque."""
    bars = _series(80, seed=92)
    reg = IndicatorRegistry()
    for n in range(29, 81):
        reg.adx(bars[:n], period=14)
    replay = reg.adx(bars[:40], period=14)
    fresh = IndicatorRegistry().adx(bars[:40], period=14)
    assert replay == pytest.approx(fresh, rel=0, abs=1e-12)


def test_adx_same_bar_returns_cached_value() -> None:
    bars = _series(60, seed=93)
    reg = IndicatorRegistry()
    first = reg.adx(bars, period=14)
    second = reg.adx(bars, period=14)
    assert first == second


def test_adx_streaming_keeps_window_bounded() -> None:
    """The cached DM/TR deque must stay bounded at ``period`` regardless of how
    many bars stream through — otherwise per-call cost drifts back to O(N)."""
    bars = _series(300, seed=94)
    reg = IndicatorRegistry()
    for n in range(29, len(bars) + 1):
        reg.adx(bars[:n], period=14)
    cached = reg._state[("adx", 14)]
    assert len(cached["dms"]) == 14


def test_bollinger_bands_round_trip_through_select() -> None:
    bars = _series(40, seed=13)
    reg = IndicatorRegistry()
    middle = reg.bollinger_bands(bars, period=20, select="middle")
    upper = reg.bollinger_bands(bars, period=20, select="upper")
    lower = reg.bollinger_bands(bars, period=20, select="lower")
    # Symmetric around the middle.
    assert upper - middle == pytest.approx(middle - lower, rel=0, abs=1e-12)


def test_bollinger_bands_streaming_matches_cold_start() -> None:
    """Driving bar-by-bar (warm path) must yield the same value as a fresh
    cold-start registry on the same slice.

    Both paths use the same running sum-of-squares formula
    ``sum_sq/period - mean²``; this test confirms that retaining state
    across bar advances produces bit-identical results compared to
    discarding and recomputing state from scratch each time.
    """
    bars = _series(80, seed=95)
    reg_streaming = IndicatorRegistry()
    period = 20
    for n in range(period, len(bars) + 1):
        sub = bars[:n]
        streaming_mid = reg_streaming.bollinger_bands(sub, period=period, select="middle")
        streaming_up = reg_streaming.bollinger_bands(sub, period=period, select="upper")
        streaming_lo = reg_streaming.bollinger_bands(sub, period=period, select="lower")
        cold_mid = IndicatorRegistry().bollinger_bands(sub, period=period, select="middle")
        cold_up = IndicatorRegistry().bollinger_bands(sub, period=period, select="upper")
        cold_lo = IndicatorRegistry().bollinger_bands(sub, period=period, select="lower")
        assert streaming_mid == pytest.approx(cold_mid, rel=1e-12), f"n={n} middle diverged"
        assert streaming_up == pytest.approx(cold_up, rel=1e-12), f"n={n} upper diverged"
        assert streaming_lo == pytest.approx(cold_lo, rel=1e-12), f"n={n} lower diverged"


def test_bollinger_bands_sliding_window_matches_cold_start() -> None:
    """A registry driven with a fixed-length sliding window must agree with
    a fresh cold-compute on each slide — the bounded deque trims correctly."""
    bars = _series(120, seed=96)
    window_size = 40
    reg = IndicatorRegistry()
    for offset in range(0, len(bars) - window_size + 1):
        sliding = bars[offset : offset + window_size]
        streaming = reg.bollinger_bands(sliding, period=20, select="upper")
        cold = IndicatorRegistry().bollinger_bands(sliding, period=20, select="upper")
        assert streaming == pytest.approx(cold, rel=1e-12), f"offset={offset} upper diverged"


def test_bollinger_bands_streaming_keeps_window_bounded() -> None:
    """The cached deque must stay bounded at ``period`` after many bars so
    per-call cost never drifts back to O(N_bars)."""
    bars = _series(300, seed=97)
    reg = IndicatorRegistry()
    period = 20
    for n in range(period, len(bars) + 1):
        reg.bollinger_bands(bars[:n], period=period, select="middle")
    cached = reg._state[("bollinger_bands", period, 2.0, "close")]
    assert len(cached["vals"]) == period


def test_bollinger_bands_replay_falls_back_to_cold_start() -> None:
    """Feeding a shorter history forces a cold-start fallback — the running
    sums must be rebuilt rather than carried forward from the longer window."""
    bars = _series(60, seed=98)
    reg = IndicatorRegistry()
    for n in range(20, 61):
        reg.bollinger_bands(bars[:n], period=20, select="middle")
    replay = reg.bollinger_bands(bars[:25], period=20, select="middle")
    fresh = IndicatorRegistry().bollinger_bands(bars[:25], period=20, select="middle")
    assert replay == pytest.approx(fresh, rel=1e-12)


def _legacy_stochastic_k(bars: List[_Bar], k_period: int) -> float:
    """Cold reference for %K at bars[-1]."""
    window = bars[-k_period:]
    lowest = min(b.low for b in window)
    highest = max(b.high for b in window)
    rng = highest - lowest
    if rng == 0:
        return 50.0
    return 100.0 * (bars[-1].close - lowest) / rng


def _legacy_stochastic_d(bars: List[_Bar], k_period: int, d_period: int) -> float | None:
    """Cold reference for %D at bars[-1]."""
    if len(bars) < k_period + d_period - 1:
        return None
    k_vals = [_legacy_stochastic_k(bars[: end + 1], k_period) for end in range(len(bars) - d_period, len(bars))]
    return sum(k_vals) / d_period


def test_stochastic_returns_k_and_d() -> None:
    bars = _series(30, seed=14)
    reg = IndicatorRegistry()
    k = reg.stochastic(bars, k_period=14, d_period=3, select="k")
    d = reg.stochastic(bars, k_period=14, d_period=3, select="d")
    assert k is not None
    assert d is not None
    assert 0.0 <= k <= 100.0
    assert 0.0 <= d <= 100.0


def test_stochastic_streaming_matches_cold_start() -> None:
    """Driving bar-by-bar must yield the same %K and %D as a fresh cold-start
    registry on the same slice."""
    bars = _series(80, seed=99)
    reg = IndicatorRegistry()
    k_period, d_period = 14, 3
    for n in range(k_period, len(bars) + 1):
        sub = bars[:n]
        stream_k = reg.stochastic(sub, k_period=k_period, d_period=d_period, select="k")
        stream_d = reg.stochastic(sub, k_period=k_period, d_period=d_period, select="d")
        ref_k = _legacy_stochastic_k(sub, k_period)
        ref_d = _legacy_stochastic_d(sub, k_period, d_period)
        assert stream_k == pytest.approx(ref_k, rel=0, abs=1e-12), f"n={n} %K diverged"
        if ref_d is None:
            assert stream_d is None, f"n={n} expected %D=None"
        else:
            assert stream_d == pytest.approx(ref_d, rel=0, abs=1e-12), f"n={n} %D diverged"


def test_stochastic_sliding_window_matches_cold_start() -> None:
    """A registry driven with a fixed-length sliding window must agree with
    a fresh cold-compute on each slide."""
    bars = _series(120, seed=100)
    window_size = 40
    reg = IndicatorRegistry()
    k_period, d_period = 14, 3
    for offset in range(0, len(bars) - window_size + 1):
        sliding = bars[offset : offset + window_size]
        stream_k = reg.stochastic(sliding, k_period=k_period, d_period=d_period, select="k")
        stream_d = reg.stochastic(sliding, k_period=k_period, d_period=d_period, select="d")
        ref_k = _legacy_stochastic_k(sliding, k_period)
        ref_d = _legacy_stochastic_d(sliding, k_period, d_period)
        assert stream_k == pytest.approx(ref_k, rel=0, abs=1e-12), f"offset={offset} %K diverged"
        assert stream_d == pytest.approx(ref_d, rel=0, abs=1e-12), f"offset={offset} %D diverged"


def test_stochastic_streaming_keeps_windows_bounded() -> None:
    """Both deques must stay bounded after many bars so cost never drifts
    back to O(N_bars)."""
    bars = _series(300, seed=101)
    reg = IndicatorRegistry()
    k_period, d_period = 14, 3
    for n in range(k_period, len(bars) + 1):
        reg.stochastic(bars[:n], k_period=k_period, d_period=d_period, select="d")
    cached = reg._state[("stochastic", k_period, d_period)]
    assert len(cached["bars_dq"]) == k_period
    assert len(cached["k_dq"]) == d_period


def test_stochastic_replay_falls_back_to_cold_start() -> None:
    """Feeding a shorter history forces a cold-start that rebuilds both deques."""
    bars = _series(80, seed=102)
    reg = IndicatorRegistry()
    for n in range(16, 81):
        reg.stochastic(bars[:n], k_period=14, d_period=3, select="d")
    replay_k = reg.stochastic(bars[:20], k_period=14, d_period=3, select="k")
    replay_d = reg.stochastic(bars[:20], k_period=14, d_period=3, select="d")
    fresh_k = IndicatorRegistry().stochastic(bars[:20], k_period=14, d_period=3, select="k")
    fresh_d = IndicatorRegistry().stochastic(bars[:20], k_period=14, d_period=3, select="d")
    assert replay_k == pytest.approx(fresh_k, rel=0, abs=1e-12)
    assert replay_d == pytest.approx(fresh_d, rel=0, abs=1e-12)


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
