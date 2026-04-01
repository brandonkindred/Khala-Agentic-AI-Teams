"""Shared trade simulation engine used by both BacktestingAgent and PaperTradingAgent.

Consolidates the bar-walking loop, position tracking, slippage/cost simulation,
force-close logic, metrics computation, and LLM call pre-filtering into a single
reusable module.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date as date_cls
from typing import Any, Callable, Dict, List, Optional

from .market_data_service import OHLCVBar
from .models import BacktestResult, StrategySpec, TradeRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------


@dataclass
class OpenPosition:
    """Typed container for an open trading position."""

    symbol: str
    side: str  # "long" or "short"
    entry_date: str
    entry_price: float
    shares: float
    position_value: float


# Callback signature: (symbol, bar, recent_bars, open_position, capital) -> decision dict
EvaluateCallback = Callable[
    [str, OHLCVBar, List[OHLCVBar], Optional[OpenPosition], float],
    Dict[str, Any],
]


# ---------------------------------------------------------------------------
# Shared LLM-based bar evaluation prompt and callback builder
# ---------------------------------------------------------------------------

_EVALUATE_PROMPT = """\
Evaluate whether the trading strategy's rules are triggered for this market data.

## Strategy
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}
Sizing rules: {sizing_rules}
Risk limits: {risk_limits}

## Current Position
{position_status}

## Available Capital
${capital:,.2f}

## Current Bar ({symbol}, {current_date})
Open: {open}  High: {high}  Low: {low}  Close: {close}  Volume: {volume}

## Recent Price History ({symbol}, last {n_bars} bars)
{recent_bars_text}

## Instructions
Based on the strategy rules and market data above, decide your action.
If you have NO open position, evaluate ENTRY rules. If you HAVE an open position, evaluate EXIT rules.
Be conservative — only enter when signals are clearly met, and respect risk limits.

Return ONLY a JSON object with no markdown:
{{"action": "enter_long" or "enter_short" or "exit" or "hold", "confidence": 0.0 to 1.0, \
"shares": number_of_shares_or_0, "reasoning": "brief explanation"}}

For "hold", set shares to 0. For entries, calculate shares based on sizing rules and available capital.
For exits, set shares to 0 (will close full position).
"""


def evaluate_bar(
    llm_complete_json: Callable[..., Dict[str, Any]],
    strategy: StrategySpec,
    system_prompt: str,
    symbol: str,
    current_bar: OHLCVBar,
    recent_bars: List[OHLCVBar],
    open_position: Optional[OpenPosition],
    capital: float,
) -> Dict[str, Any]:
    """Shared LLM-based bar evaluation used by both backtesting and paper trading agents.

    Args:
        llm_complete_json: Bound ``LLMClient.complete_json`` method.
        strategy: The strategy spec being evaluated.
        system_prompt: Agent-specific system prompt (backtest vs paper context).
        symbol: Current symbol being evaluated.
        current_bar: The OHLCV bar to evaluate.
        recent_bars: Recent price history for context.
        open_position: Current open position, if any.
        capital: Available capital for sizing.

    Returns:
        Dict with keys: action, confidence, shares, reasoning.
    """
    if open_position:
        pos_status = (
            f"OPEN {open_position.side.upper()} position in {symbol}: "
            f"{open_position.shares} shares @ ${open_position.entry_price:.2f} "
            f"(entered {open_position.entry_date})"
        )
    else:
        pos_status = "No open position — looking for entry signals."

    prompt = _EVALUATE_PROMPT.format(
        asset_class=strategy.asset_class,
        hypothesis=strategy.hypothesis,
        signal_definition=strategy.signal_definition,
        entry_rules="; ".join(strategy.entry_rules),
        exit_rules="; ".join(strategy.exit_rules),
        sizing_rules="; ".join(strategy.sizing_rules),
        risk_limits=strategy.risk_limits,
        position_status=pos_status,
        capital=capital,
        symbol=symbol,
        current_date=current_bar.date,
        open=current_bar.open,
        high=current_bar.high,
        low=current_bar.low,
        close=current_bar.close,
        volume=current_bar.volume,
        n_bars=len(recent_bars),
        recent_bars_text=format_bars_table(recent_bars),
    )

    data = llm_complete_json(
        prompt,
        temperature=0.2,
        system_prompt=system_prompt,
        think=True,
    )

    return {
        "action": str(data.get("action", "hold")),
        "confidence": float(data.get("confidence", 0.0)),
        "shares": float(data.get("shares", 0)),
        "reasoning": str(data.get("reasoning", "")),
    }


# ---------------------------------------------------------------------------
# Simulation result
# ---------------------------------------------------------------------------


@dataclass
class SimulationResult:
    """Container for results from a trade simulation run."""

    trades: List[TradeRecord] = field(default_factory=list)
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    final_capital: float = 0.0
    forced_close_count: int = 0
    evaluations_performed: int = 0
    bars_skipped_by_filter: int = 0


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def date_diff_days(d1: str, d2: str) -> int:
    """Compute days between two ISO date strings. Returns at least 1."""
    try:
        dt1 = date_cls.fromisoformat(d1[:10])
        dt2 = date_cls.fromisoformat(d2[:10])
        return max(1, abs((dt2 - dt1).days))
    except (ValueError, TypeError):
        return 1


def format_bars_table(bars: List[OHLCVBar]) -> str:
    """Format a list of OHLCV bars as a compact text table."""
    if not bars:
        return "No data available."
    lines = ["Date       | Open     | High     | Low      | Close    | Volume"]
    for b in bars:
        lines.append(
            f"{b.date} | {b.open:<8.2f} | {b.high:<8.2f} | {b.low:<8.2f} | {b.close:<8.2f} | {b.volume:.0f}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------


def compute_metrics(
    trades: List[TradeRecord],
    initial_capital: float,
    start_date: str,
    end_date: str,
) -> BacktestResult:
    """Compute aggregate performance metrics from trade records.

    Uses CAGR for annualized returns and an equity-curve approach for volatility,
    scaling by the average inter-trade gap rather than assuming daily returns.
    """
    if not trades:
        return BacktestResult(
            total_return_pct=0.0,
            annualized_return_pct=0.0,
            volatility_pct=0.0,
            sharpe_ratio=0.0,
            max_drawdown_pct=0.0,
            win_rate_pct=0.0,
            profit_factor=0.0,
        )

    wins = [t for t in trades if t.outcome == "win"]
    losses = [t for t in trades if t.outcome == "loss"]

    # Total return
    total_pnl = sum(t.net_pnl for t in trades)
    total_return_frac = total_pnl / initial_capital
    total_return_pct = round(total_return_frac * 100, 2)

    # CAGR-based annualized return
    total_days = date_diff_days(start_date, end_date)
    years = max(total_days / 365.25, 0.01)
    if total_return_frac > -1.0:
        annualized_return = round(((1 + total_return_frac) ** (1 / years) - 1) * 100, 2)
    else:
        annualized_return = -100.0

    # Win rate
    win_rate = round(len(wins) / len(trades) * 100, 2)

    # Profit factor
    gross_wins = sum(t.gross_pnl for t in wins) if wins else 0.0
    gross_losses = abs(sum(t.gross_pnl for t in losses)) if losses else 0.0
    profit_factor = (
        round(gross_wins / gross_losses, 2) if gross_losses > 0 else round(max(gross_wins, 0.0), 2)
    )

    # Volatility from equity curve at trade exits.
    # Compute returns between consecutive trade exits and annualize by the
    # average gap between exits, avoiding the incorrect assumption that
    # per-trade returns are daily returns.
    exit_equities: List[tuple[str, float]] = []
    equity = initial_capital
    for t in trades:
        equity += t.net_pnl
        exit_equities.append((t.exit_date, equity))

    annualized_vol = 0.0
    if len(exit_equities) > 1:
        inter_trade_returns: List[float] = []
        prev_eq = initial_capital
        for _, eq in exit_equities:
            if prev_eq > 0:
                inter_trade_returns.append((eq - prev_eq) / prev_eq)
            prev_eq = eq

        if len(inter_trade_returns) > 1:
            mean_r = sum(inter_trade_returns) / len(inter_trade_returns)
            var = sum((r - mean_r) ** 2 for r in inter_trade_returns) / (
                len(inter_trade_returns) - 1
            )
            total_span = date_diff_days(exit_equities[0][0], exit_equities[-1][0])
            avg_gap = max(total_span / len(inter_trade_returns), 1)
            periods_per_year = 365.25 / avg_gap
            annualized_vol = round(math.sqrt(var * periods_per_year), 2)

    # Sharpe ratio
    sharpe = round(annualized_return / annualized_vol, 2) if annualized_vol > 0 else 0.0

    # Max drawdown
    peak = initial_capital
    max_dd = 0.0
    equity = initial_capital
    for t in trades:
        equity += t.net_pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    return BacktestResult(
        total_return_pct=total_return_pct,
        annualized_return_pct=annualized_return,
        volatility_pct=annualized_vol,
        sharpe_ratio=sharpe,
        max_drawdown_pct=round(max_dd, 2),
        win_rate_pct=win_rate,
        profit_factor=profit_factor,
    )


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------


class TradeSimulationEngine:
    """Walks through OHLCV bars chronologically and simulates trade execution.

    Accepts an ``evaluate_fn`` callback (typically an LLM call) that decides
    entry/exit for each bar.  A pre-filter skips bars with minimal price
    movement when no position is held, substantially reducing evaluate calls.
    """

    def __init__(
        self,
        initial_capital: float = 100_000.0,
        transaction_cost_bps: float = 5.0,
        slippage_bps: float = 2.0,
        *,
        min_history_bars: int = 5,
        pre_filter_pct: float = 1.0,
        max_evaluations: int = 5000,
    ) -> None:
        self.initial_capital = initial_capital
        self.cost_pct = transaction_cost_bps / 10_000.0
        self.slippage_bps = slippage_bps
        self.min_history_bars = min_history_bars
        self.pre_filter_pct = pre_filter_pct
        self.max_evaluations = max_evaluations

    # ------------------------------------------------------------------

    def run(
        self,
        market_data: Dict[str, List[OHLCVBar]],
        evaluate_fn: EvaluateCallback,
        *,
        max_trades: Optional[int] = None,
        record_decisions: bool = False,
    ) -> SimulationResult:
        """Walk through market data bar-by-bar, executing trades via *evaluate_fn*.

        Args:
            market_data: ``{symbol: [OHLCVBar, ...]}``
            evaluate_fn: callback returning ``{action, confidence, shares, reasoning}``
            max_trades: stop after this many completed trades (``None`` = no limit)
            record_decisions: store every evaluation result in ``SimulationResult.decisions``
        """
        # Build unified timeline sorted by date
        timeline: List[tuple[str, str, OHLCVBar]] = []
        for symbol, bars in market_data.items():
            for bar in bars:
                timeline.append((bar.date, symbol, bar))
        timeline.sort(key=lambda x: x[0])

        symbol_history: Dict[str, List[OHLCVBar]] = {sym: [] for sym in market_data}

        capital = self.initial_capital
        open_positions: Dict[str, OpenPosition] = {}
        trades: List[TradeRecord] = []
        decisions: List[Dict[str, Any]] = []
        trade_num = 0
        cumulative_pnl = 0.0
        evaluations = 0
        skipped = 0

        for bar_date, symbol, bar in timeline:
            symbol_history[symbol].append(bar)
            recent = symbol_history[symbol][-20:]
            has_position = symbol in open_positions

            # Check evaluation budget
            if evaluations >= self.max_evaluations:
                logger.warning(
                    "Reached max evaluations (%d), stopping simulation with %d trades",
                    self.max_evaluations,
                    len(trades),
                )
                break

            # Pre-filter: skip bars unlikely to trigger signals
            if not self._should_evaluate(recent, has_position):
                skipped += 1
                continue

            # Call the evaluate callback
            try:
                decision = evaluate_fn(symbol, bar, recent, open_positions.get(symbol), capital)
            except Exception as exc:
                logger.warning("Evaluation failed for %s on %s: %s", symbol, bar_date, exc)
                decision = {
                    "action": "hold",
                    "confidence": 0.0,
                    "shares": 0,
                    "reasoning": f"Error: {exc}",
                }

            evaluations += 1
            if record_decisions:
                decisions.append({"date": bar_date, "symbol": symbol, **decision})

            if evaluations % 500 == 0:
                logger.info(
                    "Simulation progress: %d evaluations, %d trades completed",
                    evaluations,
                    len(trades),
                )

            action = decision.get("action", "hold")

            # --- Entry ---
            if action in ("enter_long", "enter_short") and not has_position:
                shares = float(decision.get("shares", 0))
                if shares <= 0:
                    position_pct = 0.06
                    shares = round(capital * position_pct / bar.close, 4 if bar.close < 10 else 2)

                if shares > 0 and capital >= shares * bar.close:
                    slippage_mult = 1.0 + self.slippage_bps / 10_000.0
                    entry_price = round(bar.close * slippage_mult, 4 if bar.close < 10 else 2)
                    position_value = round(entry_price * shares, 2)
                    capital -= position_value

                    open_positions[symbol] = OpenPosition(
                        symbol=symbol,
                        side="long" if action == "enter_long" else "short",
                        entry_date=bar_date,
                        entry_price=entry_price,
                        shares=shares,
                        position_value=position_value,
                    )

            # --- Exit ---
            elif action == "exit" and has_position:
                pos = open_positions.pop(symbol)
                trade_num += 1
                trade = self._close_position(pos, bar.close, bar_date, trade_num, cumulative_pnl)
                cumulative_pnl = trade.cumulative_pnl
                capital += round(pos.shares * trade.exit_price, 2)
                trades.append(trade)

            # Check trade limit
            if max_trades is not None and len(trades) >= max_trades:
                break

        # Force-close remaining open positions
        forced = 0
        for symbol, pos in list(open_positions.items()):
            if symbol in symbol_history and symbol_history[symbol]:
                last_bar = symbol_history[symbol][-1]
                trade_num += 1
                trade = self._close_position(
                    pos, last_bar.close, last_bar.date, trade_num, cumulative_pnl
                )
                cumulative_pnl = trade.cumulative_pnl
                capital += round(pos.shares * trade.exit_price, 2)
                trades.append(trade)
                forced += 1

        return SimulationResult(
            trades=trades,
            decisions=decisions,
            final_capital=capital,
            forced_close_count=forced,
            evaluations_performed=evaluations,
            bars_skipped_by_filter=skipped,
        )

    # ------------------------------------------------------------------

    def _should_evaluate(self, recent_bars: List[OHLCVBar], has_position: bool) -> bool:
        """Pre-filter: decide whether this bar warrants an LLM evaluation.

        Always evaluates when a position is open (exit signals).  For entry
        signals, requires minimum history and meaningful price movement
        relative to the recent average.
        """
        if has_position:
            return True
        if len(recent_bars) < self.min_history_bars:
            return False
        closes = [b.close for b in recent_bars[-self.min_history_bars :]]
        avg = sum(closes) / len(closes)
        if avg <= 0:
            return True
        move_pct = abs(recent_bars[-1].close - avg) / avg * 100
        return move_pct >= self.pre_filter_pct

    def _close_position(
        self,
        pos: OpenPosition,
        close_price: float,
        exit_date: str,
        trade_num: int,
        cumulative_pnl: float,
    ) -> TradeRecord:
        """Close a position and return a ``TradeRecord``."""
        slippage_mult = 1.0 - self.slippage_bps / 10_000.0
        exit_price = round(close_price * slippage_mult, 4 if close_price < 10 else 2)

        gross_pnl = round(pos.shares * (exit_price - pos.entry_price), 2)
        if pos.side == "short":
            gross_pnl = -gross_pnl

        tx_cost = round(pos.position_value * self.cost_pct * 2, 2)
        net_pnl = round(gross_pnl - tx_cost, 2)
        cumulative_pnl = round(cumulative_pnl + net_pnl, 2)

        return_pct = round((exit_price - pos.entry_price) / pos.entry_price * 100, 3)
        if pos.side == "short":
            return_pct = -return_pct

        hold_days = date_diff_days(pos.entry_date, exit_date)

        return TradeRecord(
            trade_num=trade_num,
            entry_date=pos.entry_date,
            exit_date=exit_date,
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            shares=pos.shares,
            position_value=pos.position_value,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
            return_pct=return_pct,
            hold_days=hold_days,
            outcome="win" if net_pnl > 0 else "loss",
            cumulative_pnl=cumulative_pnl,
        )
