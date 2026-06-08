"""Unit tests for the shared synthetic-bar helpers in ``conformance_bars``.

These four utilities (OHLC clamping, DataFrame conversion, universe-symbol
resolution, ``UNIVERSE`` literal parsing) were extracted from the retired
rule-probes synthesizer and are still used by the predicate-conformance gate.
The tests preserve the coverage the deleted probe suite provided.
"""

from __future__ import annotations

from investment_team.market_data_service import OHLCVBar
from investment_team.strategy_lab.quality_gates.conformance_bars import (
    _MIN_PRICE,
    _PROBE_SYMBOL_FALLBACK,
    _bars_to_df,
    _extract_universe_literal,
    _normalise_ohlc,
    _resolve_probe_symbol,
)


def _bar(**kw) -> OHLCVBar:
    base = dict(date="2024-01-01", open=10.0, high=12.0, low=8.0, close=11.0, volume=1000.0)
    base.update(kw)
    return OHLCVBar(**base)


# ---------------------------------------------------------------------------
# _normalise_ohlc
# ---------------------------------------------------------------------------


def test_normalise_ohlc_clamps_negative_values() -> None:
    out = _normalise_ohlc(_bar(open=-5.0, high=-1.0, low=-9.0, close=-3.0))
    assert out.open >= _MIN_PRICE
    assert out.high >= max(out.open, out.close, out.low)
    assert out.low <= min(out.open, out.close, out.high)


def test_normalise_ohlc_replaces_nan_volume_with_one() -> None:
    out = _normalise_ohlc(_bar(volume=float("nan")))
    assert out.volume == 1.0


def test_normalise_ohlc_handles_non_finite_field() -> None:
    # The ``_safe`` None/non-finite branch: NaN falls back to the floor.
    out = _normalise_ohlc(_bar(open=float("nan")))
    assert out.open == _MIN_PRICE


def test_normalise_ohlc_preserves_clean_bars() -> None:
    out = _normalise_ohlc(_bar())
    assert (out.open, out.high, out.low, out.close) == (10.0, 12.0, 8.0, 11.0)


def test_normalise_ohlc_enforces_invariants() -> None:
    # high below the body / low above the body get repaired.
    out = _normalise_ohlc(_bar(open=10.0, close=11.0, high=9.0, low=12.0))
    assert out.high >= max(out.open, out.close, out.low)
    assert out.low <= min(out.open, out.close, out.high)


def test_normalise_ohlc_clamps_negative_volume() -> None:
    out = _normalise_ohlc(_bar(volume=-50.0))
    assert out.volume == 1.0


# ---------------------------------------------------------------------------
# _bars_to_df
# ---------------------------------------------------------------------------


def test_bars_to_df_columns_and_order() -> None:
    bars = [_bar(close=1.0), _bar(close=2.0), _bar(close=3.0)]
    df = _bars_to_df(bars)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert list(df["close"]) == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# _resolve_probe_symbol
# ---------------------------------------------------------------------------


class _SpecStub:
    def __init__(self, target_symbols):
        self.target_symbols = target_symbols


def test_resolve_probe_symbol_prefers_target_symbols() -> None:
    code = "UNIVERSE = frozenset({'MSFT'})\n"
    assert _resolve_probe_symbol(_SpecStub(["AAPL"]), code) == "AAPL"


def test_resolve_probe_symbol_falls_back_to_universe_literal() -> None:
    code = "UNIVERSE = frozenset({'MSFT'})\n"
    assert _resolve_probe_symbol(_SpecStub([]), code) == "MSFT"


def test_resolve_probe_symbol_falls_back_to_sentinel() -> None:
    assert _resolve_probe_symbol(_SpecStub([]), "x = 1\n") == _PROBE_SYMBOL_FALLBACK


# ---------------------------------------------------------------------------
# _extract_universe_literal
# ---------------------------------------------------------------------------


def test_extract_universe_plain_assign() -> None:
    assert _extract_universe_literal("UNIVERSE = frozenset({'AAPL', 'MSFT'})\n") == frozenset(
        {"AAPL", "MSFT"}
    )


def test_extract_universe_annotated_assign() -> None:
    code = "UNIVERSE: frozenset[str] = frozenset({'AAPL'})\n"
    assert _extract_universe_literal(code) == frozenset({"AAPL"})


def test_extract_universe_self_attribute_assign() -> None:
    code = "class S:\n    def __init__(self):\n        self.UNIVERSE = frozenset({'AAPL'})\n"
    assert _extract_universe_literal(code) == frozenset({"AAPL"})


def test_extract_universe_bare_annotation_without_value() -> None:
    assert _extract_universe_literal("UNIVERSE: frozenset[str]\n") == frozenset()


def test_extract_universe_empty_frozenset() -> None:
    assert _extract_universe_literal("UNIVERSE = frozenset()\n") == frozenset()


def test_extract_universe_returns_empty_on_syntax_error() -> None:
    assert _extract_universe_literal("def broken(:\n") == frozenset()


def test_extract_universe_returns_empty_on_non_call_assignment() -> None:
    assert _extract_universe_literal("UNIVERSE = 5\n") == frozenset()


def test_extract_universe_returns_empty_when_no_assignment() -> None:
    assert _extract_universe_literal("x = frozenset({'AAPL'})\n") == frozenset()


def test_extract_universe_empty_code() -> None:
    assert _extract_universe_literal("") == frozenset()


def test_extract_universe_skips_multi_target_assign() -> None:
    assert _extract_universe_literal("UNIVERSE = OTHER = frozenset({'AAPL'})\n") == frozenset()


def test_extract_universe_non_literal_arg_returns_empty() -> None:
    # ``frozenset(some_var)`` is not a parseable literal.
    assert _extract_universe_literal("UNIVERSE = frozenset(some_var)\n") == frozenset()
