"""Unit tests for the coverage-probe aggregator's internal helpers (#451).

Direct tests on ``_to_dataframes`` and related private functions.
Stage-level integration tests live in ``test_coverage_probe_stage.py``;
gate / merge / dedup tests in ``test_coverage_probe_aggregator.py``.
"""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from investment_team.market_data_service import OHLCVBar
from investment_team.strategy_lab.coverage_probe.aggregator import _to_dataframes

from ._coverage_probe_test_helpers import make_flat_df

# ─────────────────────────────────────────────────────────────────────
# _to_dataframes — date-index parsing (R2)
# ─────────────────────────────────────────────────────────────────────


def test_to_dataframes_sets_datetime_index_when_every_date_parses() -> None:
    bars = [
        OHLCVBar(date=f"2024-01-{i + 1:02d}", open=1, high=1, low=1, close=1, volume=1)
        for i in range(5)
    ]
    df = _to_dataframes({"AAPL": bars})["AAPL"]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert len(df) == 5


def test_to_dataframes_falls_back_to_integer_index_on_any_unparseable_date() -> None:
    """If any OHLCVBar date fails to parse, the entire DataFrame keeps
    the integer index rather than producing a half-broken
    DatetimeIndex peppered with NaT.
    """
    bars = [
        OHLCVBar(date="2024-01-01", open=1, high=1, low=1, close=1, volume=1),
        OHLCVBar(date="not-a-date", open=1, high=1, low=1, close=1, volume=1),
        OHLCVBar(date="2024-01-03", open=1, high=1, low=1, close=1, volume=1),
    ]
    df = _to_dataframes({"AAPL": bars})["AAPL"]
    assert isinstance(df, pd.DataFrame)
    assert not isinstance(df.index, pd.DatetimeIndex)
    assert len(df) == 3


# ─────────────────────────────────────────────────────────────────────
# _to_dataframes — malformed entries are dropped with a debug log (R3)
# ─────────────────────────────────────────────────────────────────────


def test_to_dataframes_drops_malformed_entries_with_debug_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Anything that isn't a DataFrame or a non-empty list of OHLCVBars
    is dropped from the indicator-probe input — and the drop is logged
    at DEBUG so future debugging isn't a silent mystery.
    """
    with caplog.at_level(
        logging.DEBUG, logger="investment_team.strategy_lab.coverage_probe.aggregator"
    ):
        out = _to_dataframes(
            {
                "GOOD": make_flat_df(10),
                "BAD_NONE": None,  # type: ignore[dict-item]
                "BAD_DICT": {"close": 1},  # type: ignore[dict-item]
                "BAD_EMPTY": [],
            }
        )

    assert set(out.keys()) == {"GOOD"}
    dropped_symbols = {
        sym
        for r in caplog.records
        if "dropping market_data entry" in r.message
        for sym in ("BAD_NONE", "BAD_DICT", "BAD_EMPTY")
        if repr(sym) in r.message
    }
    assert dropped_symbols == {"BAD_NONE", "BAD_DICT", "BAD_EMPTY"}


# ─────────────────────────────────────────────────────────────────────
# _to_dataframes — DataFrame inputs pass through by identity
# ─────────────────────────────────────────────────────────────────────


def test_to_dataframes_passes_dataframe_inputs_through_by_identity() -> None:
    """The conversion is opportunistic: a caller that already provides a
    DataFrame must see it forwarded unchanged (same object, not a copy)."""
    df = make_flat_df(10)
    out = _to_dataframes({"AAPL": df})
    assert out["AAPL"] is df
