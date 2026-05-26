"""Shared OHLCV DataFrame builders + strategy snippet helper for indicator-probe tests.

Not a test module — the leading underscore keeps pytest from collecting it.
"""

from __future__ import annotations

import textwrap

import numpy as np
import pandas as pd


def flat_ohlcv(n: int = 60, base: float = 100.0) -> pd.DataFrame:
    """Flat OHLCV at *base* price for *n* bars."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {
            "open": np.full(n, base),
            "high": np.full(n, base + 1.0),
            "low": np.full(n, base - 1.0),
            "close": np.full(n, base),
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def swing_ohlcv(n: int = 200, leg: int = 50, step: float = 0.005) -> pd.DataFrame:
    """Sawtooth price series that drives RSI to its extremes."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves: list[float] = []
    while len(moves) < n:
        moves.extend([-step] * leg)
        moves.extend([+step] * leg)
    moves = moves[:n]
    close = 100.0 * np.cumprod(1.0 + np.array(moves))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def flat_close_df(close_value: float, n: int = 30) -> pd.DataFrame:
    """Flat OHLCV at arbitrary *close_value* for symbol-gated tests."""
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


def swing_close_df(n: int = 100) -> pd.DataFrame:
    """Sharp alternating moves for ATR-divergence tests."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
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


def small_swing_df(n: int = 30) -> pd.DataFrame:
    """30-bar swing fixture for period-shadowing / scope tests."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    moves = [-0.005] * 8 + [+0.005] * 8 + [-0.005] * 7 + [+0.005] * 7
    close = 100.0 * np.cumprod(1.0 + np.array(moves[:n]))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        },
        index=idx,
    )


def make_strategy(predicate: str, *, preamble: str = "", init: str = "") -> str:
    """Build a minimal strategy class with the given ``on_bar`` predicate.

    *preamble* is placed before the class (module-level code).
    *init* is placed inside an ``__init__`` method body.
    """
    parts: list[str] = []
    if preamble:
        parts.append(preamble)
        parts.append("")
    parts.append("class S:")
    if init:
        parts.append("    def __init__(self):")
        for line in init.splitlines():
            parts.append(f"        {line}")
        parts.append("")
    parts.append("    def on_bar(self, ctx, bar):")
    parts.append(f"        if {predicate}:")
    parts.append("            pass")
    return textwrap.dedent("\n".join(parts))
