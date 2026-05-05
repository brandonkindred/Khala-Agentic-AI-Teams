"""Snapshot tests for reference strategies on the golden dataset.

Runs two deterministic strategies end-to-end through the subprocess harness and
asserts that the resulting metrics match a stored JSON snapshot.  Snapshots
live next to this module under ``snapshots/`` and are written automatically
the first time a test runs against a new reference output; updating a
snapshot is a deliberate two-step act (delete the file, re-run).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from investment_team.models import BacktestConfig, StrategySpec, TradeRecord
from investment_team.trading_service.modes.backtest import run_backtest

from .fixtures import DEFAULT_DAYS, golden_market_data
from .strategies import OVERFIT_HARDCODED_DATES_CODE, ROUND_TRIP_CODE, SMA_CROSSOVER_CODE

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
UPDATE_SNAPSHOTS = os.environ.get("UPDATE_GOLDEN_SNAPSHOTS") == "1"


def _make_spec(name: str, code: str) -> StrategySpec:
    return StrategySpec(
        strategy_id=f"golden-{name}",
        authored_by="golden-tests",
        asset_class="stocks",
        hypothesis="deterministic reference",
        signal_definition="see strategies.py",
        strategy_code=code,
    )


def _make_config() -> BacktestConfig:
    # Pin the legacy fill geometry so snapshot drift can't be introduced by
    # future tweaks to ``RealisticExecutionModel``. The reference strategies
    # used here trade only MARKET orders so optimistic vs. realistic produce
    # byte-identical fills today, but pinning makes the contract explicit.
    return BacktestConfig(
        start_date="2024-01-01",
        end_date="2024-12-31",
        initial_capital=100_000.0,
        transaction_cost_bps=0.0,
        slippage_bps=0.0,
        execution_model="optimistic",
    )


def _summarize_trades(trades: List[TradeRecord]) -> Dict[str, Any]:
    """Produce a compact, order-preserving summary of the trade list.

    Uses rounded values so cross-platform float drift doesn't flip snapshots.
    Only includes the fields that are invariant under the current engine
    contract (fill at next bar's open, close at next bar's open on exit).
    """
    return {
        "count": len(trades),
        "trades": [
            {
                "trade_num": t.trade_num,
                "symbol": t.symbol,
                "side": t.side,
                "entry_date": t.entry_date,
                "exit_date": t.exit_date,
                "shares": round(t.shares, 4),
                "gross_pnl": round(t.gross_pnl, 2),
                "net_pnl": round(t.net_pnl, 2),
                "hold_days": t.hold_days,
                "outcome": t.outcome,
            }
            for t in trades
        ],
    }


def _assert_snapshot(name: str, payload: Dict[str, Any]) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.json"
    serialized = json.dumps(payload, indent=2, sort_keys=True)
    if UPDATE_SNAPSHOTS or not path.exists():
        path.write_text(serialized + "\n", encoding="utf-8")
        if not UPDATE_SNAPSHOTS:
            pytest.skip(
                f"wrote initial snapshot {path.name}; re-run tests to compare "
                "against it.  Commit the new snapshot file if the numbers are "
                "intentional."
            )
        return
    expected = path.read_text(encoding="utf-8").rstrip() + "\n"
    assert serialized + "\n" == expected, (
        f"Snapshot drift for {name}. Set UPDATE_GOLDEN_SNAPSHOTS=1 to accept "
        f"the new values (after verifying the change is intentional)."
    )


def _run(name: str, code: str) -> Dict[str, Any]:
    spec = _make_spec(name, code)
    config = _make_config()
    market_data = golden_market_data(n_days=DEFAULT_DAYS)
    result = run_backtest(strategy=spec, config=config, market_data=market_data)

    return {
        "config": {
            "start_date": config.start_date,
            "end_date": config.end_date,
            "initial_capital": config.initial_capital,
            "symbols": sorted(market_data.keys()),
            "bars_per_symbol": DEFAULT_DAYS,
        },
        "trades": _summarize_trades(result.trades),
        "metrics_daily": result.result.model_dump(),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.slow_subprocess
def test_sma_crossover_snapshot() -> None:
    _assert_snapshot("sma_crossover", _run("sma_crossover", SMA_CROSSOVER_CODE))


@pytest.mark.slow_subprocess
def test_round_trip_snapshot() -> None:
    _assert_snapshot("round_trip", _run("round_trip", ROUND_TRIP_CODE))


@pytest.mark.slow_subprocess
def test_overfit_hardcoded_dates_snapshot() -> None:
    """Snapshot of the known-overfit reference on the full fixture.

    The strategy is curve-fit by construction (see strategies.py), so it will
    look great on single-window metrics — that is the point. The end-to-end
    walk-forward rejection of this strategy is covered by
    ``test_orchestrator_walk_forward.py`` (issue #247 step 8).
    """
    _assert_snapshot(
        "overfit_hardcoded_dates",
        _run("overfit_hardcoded_dates", OVERFIT_HARDCODED_DATES_CODE),
    )
