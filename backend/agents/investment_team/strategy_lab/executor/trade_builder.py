"""Convert raw trade dicts from generated code into proper TradeRecord objects.

Applies slippage and transaction costs post-hoc, matching the math from
TradeSimulationEngine._close_position() in trade_simulator.py.
"""

from __future__ import annotations

from datetime import date as date_cls
from typing import Any, Dict, List

from ...models import BacktestConfig, TradeRecord

_VALID_SIDES = frozenset({"long", "short"})


def build_trade_records(
    raw_trades: List[Dict[str, Any]],
    config: BacktestConfig,
) -> List[TradeRecord]:
    """Convert raw trade dicts into TradeRecords with slippage and costs applied.

    Each raw trade must have: symbol, side, entry_date, entry_price, exit_date,
    exit_price, shares.  Trades are sorted chronologically by exit_date (then
    entry_date as tiebreaker) before numbering and accumulating cumulative PnL,
    so that equity-curve metrics are order-independent of how the strategy code
    emitted them.

    Raises:
        ValueError: If any trade has a ``side`` other than "long" or "short".
    """
    slippage_mult = config.slippage_bps / 10_000
    cost_mult = config.transaction_cost_bps / 10_000

    # Sort chronologically so cumulative PnL / equity curve are correct
    sorted_trades = sorted(
        raw_trades,
        key=lambda t: (str(t.get("exit_date", ""))[:10], str(t.get("entry_date", ""))[:10]),
    )

    records: List[TradeRecord] = []
    cumulative_pnl = 0.0

    for i, raw in enumerate(sorted_trades):
        side = str(raw["side"]).lower().strip()
        if side not in _VALID_SIDES:
            raise ValueError(
                f"Trade {i} has invalid side '{raw['side']}'. Must be 'long' or 'short'."
            )

        entry_price = float(raw["entry_price"])
        exit_price = float(raw["exit_price"])
        shares = abs(float(raw["shares"]))
        symbol = str(raw["symbol"])
        entry_date = str(raw["entry_date"])[:10]
        exit_date = str(raw["exit_date"])[:10]

        # Apply slippage — longs: entry up / exit down; shorts: entry down / exit up
        if side == "long":
            adj_entry = entry_price * (1 + slippage_mult)
            adj_exit = exit_price * (1 - slippage_mult)
            gross_pnl = (adj_exit - adj_entry) * shares
        else:
            adj_entry = entry_price * (1 - slippage_mult)
            adj_exit = exit_price * (1 + slippage_mult)
            gross_pnl = (adj_entry - adj_exit) * shares

        position_value = adj_entry * shares

        # Transaction costs (entry + exit)
        tx_costs = position_value * cost_mult * 2
        net_pnl = gross_pnl - tx_costs

        # Return percentage
        return_pct = round((net_pnl / position_value * 100) if position_value > 0 else 0.0, 2)

        # Hold days
        hold_days = _date_diff(entry_date, exit_date)

        # Outcome
        outcome = "win" if net_pnl > 0 else "loss"

        cumulative_pnl += net_pnl

        records.append(
            TradeRecord(
                trade_num=i + 1,
                entry_date=entry_date,
                exit_date=exit_date,
                symbol=symbol,
                side=side,
                entry_price=round(adj_entry, 4),
                exit_price=round(adj_exit, 4),
                shares=round(shares, 6),
                position_value=round(position_value, 2),
                gross_pnl=round(gross_pnl, 2),
                net_pnl=round(net_pnl, 2),
                return_pct=return_pct,
                hold_days=hold_days,
                outcome=outcome,
                cumulative_pnl=round(cumulative_pnl, 2),
            )
        )

    return records


def _date_diff(d1: str, d2: str) -> int:
    """Compute days between two ISO date strings.  Returns at least 0."""
    try:
        dt1 = date_cls.fromisoformat(d1[:10])
        dt2 = date_cls.fromisoformat(d2[:10])
        return max(0, abs((dt2 - dt1).days))
    except (ValueError, TypeError):
        return 0
