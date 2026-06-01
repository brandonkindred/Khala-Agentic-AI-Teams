"""Equivalence tests for the O(n log n) entry-equity computation.

``_entry_equity_by_trade`` replaced an O(n²) per-trade rescan with a
sorted-exit-date prefix sum + binary search. These tests assert the new
implementation is value-identical to the original formula across randomized
ledgers, including the date-tie and degenerate same-bar-fill edge cases.
"""

from __future__ import annotations

import random
from types import SimpleNamespace
from typing import Dict, List

import pytest

from investment_team.strategy_lab.quality_gates.alignment_checks import (
    _entry_equity_by_trade,
)


def _reference_equity(trades: List, initial_capital: float) -> Dict[int, float]:
    """The original O(n²) definition, kept here as the oracle."""
    initial = float(initial_capital)
    out: Dict[int, float] = {}
    for trade in trades:
        realized = sum(
            float(prior.net_pnl)
            for prior in trades
            if prior.exit_date <= trade.entry_date and prior.trade_num != trade.trade_num
        )
        out[trade.trade_num] = initial + realized
    return out


def _trade(trade_num: int, entry: str, exit_: str, net_pnl: float) -> SimpleNamespace:
    return SimpleNamespace(trade_num=trade_num, entry_date=entry, exit_date=exit_, net_pnl=net_pnl)


def test_matches_reference_on_simple_sequential_ledger() -> None:
    trades = [
        _trade(1, "2023-01-01", "2023-01-05", 100.0),
        _trade(2, "2023-01-06", "2023-01-10", -40.0),
        _trade(3, "2023-01-11", "2023-01-20", 25.0),
    ]
    got = _entry_equity_by_trade(trades, 1_000.0)
    assert got == _reference_equity(trades, 1_000.0)
    # Spot value: trade 3 sees trades 1 and 2 already realized.
    assert got[3] == 1_000.0 + 100.0 - 40.0


def test_overlapping_trades_do_not_leak_future_pnl() -> None:
    # Trade A enters before B but exits AFTER B's entry — A's PnL must not
    # be in B's baseline.
    trades = [
        _trade(1, "2023-01-01", "2023-01-20", 500.0),  # A: open across B's entry
        _trade(2, "2023-01-05", "2023-01-10", 30.0),  # B: enters while A open
    ]
    got = _entry_equity_by_trade(trades, 10_000.0)
    assert got == _reference_equity(trades, 10_000.0)
    assert got[2] == 10_000.0  # A not yet exited at B's entry


def test_same_day_exit_ties_match_reference() -> None:
    # Several trades exiting on the exact same date as another's entry.
    trades = [
        _trade(1, "2023-01-01", "2023-01-05", 10.0),
        _trade(2, "2023-01-02", "2023-01-05", 20.0),
        _trade(3, "2023-01-05", "2023-01-09", 5.0),  # entry == others' exit
    ]
    got = _entry_equity_by_trade(trades, 0.0)
    assert got == _reference_equity(trades, 0.0)
    # Both prior exits (<=) count toward trade 3's baseline.
    assert got[3] == 30.0


def test_degenerate_same_bar_fill_excludes_self() -> None:
    # A trade whose exit_date <= its own entry_date must not count its own
    # PnL — mirrors the original ``trade_num`` guard.
    trades = [_trade(1, "2023-01-05", "2023-01-05", 999.0)]
    got = _entry_equity_by_trade(trades, 100.0)
    assert got == _reference_equity(trades, 100.0)
    assert got[1] == 100.0


def test_property_random_ledgers_match_reference() -> None:
    rng = random.Random(1234)
    for _ in range(200):
        n = rng.randint(1, 40)
        trades = []
        for i in range(1, n + 1):
            entry_day = rng.randint(1, 27)
            exit_day = rng.randint(entry_day, 28)  # exit on/after entry
            trades.append(
                _trade(
                    i,
                    f"2023-02-{entry_day:02d}",
                    f"2023-02-{exit_day:02d}",
                    round(rng.uniform(-500, 500), 2),
                )
            )
        rng.shuffle(trades)  # order independence
        capital = rng.choice([0.0, 1_000.0, 100_000.0])
        got = _entry_equity_by_trade(trades, capital)
        # Equal up to float summation order (prefix sum vs. per-trade re-sum
        # accumulate rounding differently — same mathematical value).
        assert got == pytest.approx(_reference_equity(trades, capital), rel=1e-9, abs=1e-6)
