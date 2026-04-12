"""
Paper Trading Agent — runs simulated live trading using LLM to interpret strategy rules against real market data.

Delegates the bar-walking simulation to :class:`TradeSimulationEngine` and adds
paper-trading-specific features: performance comparison, divergence analysis, and
verdict generation.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from strands import Agent

from llm_service import get_strands_model

from .market_data_service import OHLCVBar
from .models import (
    BacktestRecord,
    BacktestResult,
    PaperTradingComparison,
    PaperTradingSession,
    PaperTradingStatus,
    PaperTradingVerdict,
    StrategySpec,
    TradeRecord,
)
from .trade_simulator import (
    OpenPosition,
    TradeSimulationEngine,
    compute_metrics,
    evaluate_bar,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_EVALUATE_SYSTEM = (
    "You are an expert quantitative trader executing a swing trading strategy. "
    "You evaluate daily price bars against the strategy's entry and exit rules to decide whether to trade. "
    "Be disciplined — only trade when the rules are clearly met by the price data."
)

_DIVERGENCE_SYSTEM = (
    "You are a quantitative trading analyst specializing in strategy validation. "
    "You compare paper trading results against backtesting results to identify "
    "why live-data performance diverges from historical simulations."
)

_DIVERGENCE_PROMPT = """\
Analyze why this strategy's paper trading performance diverges from its backtest results.

## Strategy
Asset class: {asset_class}
Hypothesis: {hypothesis}
Signal: {signal_definition}
Entry rules: {entry_rules}
Exit rules: {exit_rules}

## Backtest Results (historical simulation)
Period: {bt_start} to {bt_end}
Annualized return: {bt_annual_return:.1f}%
Win rate: {bt_win_rate:.1f}%
Sharpe ratio: {bt_sharpe:.2f}
Max drawdown: {bt_max_dd:.1f}%
Profit factor: {bt_profit_factor:.2f}
Total trades: {bt_trade_count}

## Paper Trading Results (real market data)
Period: {pt_start} to {pt_end}
Annualized return: {pt_annual_return:.1f}%
Win rate: {pt_win_rate:.1f}%
Sharpe ratio: {pt_sharpe:.2f}
Max drawdown: {pt_max_dd:.1f}%
Profit factor: {pt_profit_factor:.2f}
Total trades: {pt_trade_count}

## Backtest Trade Sample (first 10 trades)
{bt_trades_text}

## Paper Trading Trades (first 20 trades)
{pt_trades_text}

## Instructions
Write a thorough analysis covering:
1. **Metric Divergence**: Which metrics diverged most and by how much
2. **Trade Pattern Analysis**: Compare entry/exit patterns between backtest and paper trading
3. **Root Cause Hypotheses**: Why paper trading underperforms (overfitting, regime change, \
lookahead bias, market microstructure, data quality differences, etc.)
4. **Actionable Improvements**: Specific changes to strategy rules, signals, or risk management \
that could improve future live-data performance

Return ONLY a JSON object with no markdown:
{{"analysis": "your full analysis here"}}
"""


def _format_trades_table(trades: List[TradeRecord]) -> str:
    """Format trade records as a compact text table."""
    if not trades:
        return "No trades."
    lines = [
        "# | Symbol | Side | Entry Date | Exit Date  | Entry$   | Exit$    | Return%  | Outcome"
    ]
    for t in trades:
        lines.append(
            f"{t.trade_num} | {t.symbol} | {t.side} | {t.entry_date} | {t.exit_date} | "
            f"{t.entry_price:<8.2f} | {t.exit_price:<8.2f} | {t.return_pct:+.2f}% | {t.outcome}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


def _make_agent_complete_json(agent: Agent) -> Any:
    """Return a callable compatible with evaluate_bar's llm_complete_json signature."""

    def _complete_json(prompt: str, **_kwargs: Any) -> Dict[str, Any]:
        result = agent(prompt)
        raw = str(result).strip()
        return json.loads(raw)

    return _complete_json


class PaperTradingAgent:
    """
    Runs paper trading sessions by walking through real market data bar-by-bar,
    using the LLM to interpret strategy entry/exit rules, and simulating trade execution.
    """

    def __init__(self, llm_client=None) -> None:
        self._evaluate_agent = (
            llm_client
            if llm_client is not None
            else Agent(
                model=get_strands_model("paper_trading"),
                system_prompt=_EVALUATE_SYSTEM,
            )
        )
        self._divergence_agent = (
            llm_client
            if llm_client is not None
            else Agent(
                model=get_strands_model("paper_trading"),
                system_prompt=_DIVERGENCE_SYSTEM,
            )
        )

    def run_session(
        self,
        strategy: StrategySpec,
        backtest_record: BacktestRecord,
        market_data: Dict[str, List[OHLCVBar]],
        initial_capital: float = 100_000.0,
        transaction_cost_bps: float = 5.0,
        slippage_bps: float = 2.0,
        min_trades: int = 50,
    ) -> PaperTradingSession:
        """Walk through market data bar-by-bar, making LLM-driven trade decisions."""
        session_id = f"pt-{uuid.uuid4().hex[:8]}"
        now = datetime.now(tz=timezone.utc).isoformat()

        # Determine data period
        all_dates: List[str] = []
        for bars in market_data.values():
            all_dates.extend(b.date for b in bars)
        all_dates.sort()
        data_start = all_dates[0] if all_dates else ""
        data_end = all_dates[-1] if all_dates else ""

        data_source = "yahoo_finance"

        # Count total available bars to warn if min_trades may not be reachable
        total_bars = sum(len(bars) for bars in market_data.values())

        session = PaperTradingSession(
            session_id=session_id,
            lab_record_id="",  # set by caller
            strategy=strategy,
            status=PaperTradingStatus.RUNNING,
            initial_capital=initial_capital,
            current_capital=initial_capital,
            symbols_traded=list(market_data.keys()),
            data_source=data_source,
            data_period_start=data_start,
            data_period_end=data_end,
            started_at=now,
        )

        # Run simulation via shared engine
        engine = TradeSimulationEngine(
            initial_capital=initial_capital,
            transaction_cost_bps=transaction_cost_bps,
            slippage_bps=slippage_bps,
        )

        def evaluate(
            symbol: str,
            bar: OHLCVBar,
            recent: List[OHLCVBar],
            position: Optional[OpenPosition],
            capital: float,
        ) -> Dict[str, Any]:
            return self._evaluate_bar(strategy, symbol, bar, recent, position, capital)

        sim = engine.run(market_data, evaluate, max_trades=min_trades, record_decisions=True)

        if total_bars > 0 and len(sim.trades) < min_trades:
            logger.warning(
                "Paper trading session completed with only %d/%d requested trades "
                "(%d bars available across %d symbols).",
                len(sim.trades),
                min_trades,
                total_bars,
                len(market_data),
            )

        # Populate session from simulation result
        session.trades = sim.trades
        session.trade_decisions = sim.decisions
        session.current_capital = sim.final_capital
        session.completed_at = datetime.now(tz=timezone.utc).isoformat()

        if sim.trades:
            first_date = sim.trades[0].entry_date
            last_date = sim.trades[-1].exit_date
            session.result = compute_metrics(sim.trades, initial_capital, first_date, last_date)
            session.comparison = self.compare_performance(session.result, backtest_record.result)

            if session.comparison.overall_aligned:
                session.verdict = PaperTradingVerdict.READY_FOR_LIVE
            else:
                session.verdict = PaperTradingVerdict.NOT_PERFORMANT
                try:
                    session.divergence_analysis = self.analyze_divergence(session, backtest_record)
                except Exception as exc:
                    logger.warning("Divergence analysis failed: %s", exc)
                    session.divergence_analysis = (
                        f"Paper trading did not align with backtest expectations. "
                        f"Paper win rate: {session.result.win_rate_pct:.1f}% vs "
                        f"backtest: {backtest_record.result.win_rate_pct:.1f}%. "
                        f"Automated analysis unavailable."
                    )

        session.status = PaperTradingStatus.COMPLETED
        return session

    def _evaluate_bar(
        self,
        strategy: StrategySpec,
        symbol: str,
        current_bar: OHLCVBar,
        recent_bars: List[OHLCVBar],
        open_position: Optional[OpenPosition],
        capital: float,
    ) -> Dict[str, Any]:
        """Ask LLM to evaluate whether entry/exit rules are met for this bar."""
        return evaluate_bar(
            _make_agent_complete_json(self._evaluate_agent),
            strategy,
            _EVALUATE_SYSTEM,
            symbol,
            current_bar,
            recent_bars,
            open_position,
            capital,
        )

    @staticmethod
    def compare_performance(
        paper_result: BacktestResult,
        backtest_result: BacktestResult,
    ) -> PaperTradingComparison:
        """Compare paper trading metrics against backtest expectations with tolerances."""
        # Win rate: within ±10 percentage points
        win_rate_aligned = abs(paper_result.win_rate_pct - backtest_result.win_rate_pct) <= 10.0

        # Annualized return: within ±40% relative (or ±3pp absolute for small values)
        bt_ret = backtest_result.annualized_return_pct
        pt_ret = paper_result.annualized_return_pct
        if abs(bt_ret) > 5.0:
            return_aligned = abs(pt_ret - bt_ret) / abs(bt_ret) <= 0.40
        else:
            return_aligned = abs(pt_ret - bt_ret) <= 3.0

        # Sharpe: within ±0.3
        sharpe_aligned = abs(paper_result.sharpe_ratio - backtest_result.sharpe_ratio) <= 0.3

        # Max drawdown: paper drawdown no more than 1.5x backtest drawdown
        if backtest_result.max_drawdown_pct > 0:
            drawdown_aligned = (
                paper_result.max_drawdown_pct <= backtest_result.max_drawdown_pct * 1.5
            )
        else:
            drawdown_aligned = paper_result.max_drawdown_pct <= 5.0

        overall = win_rate_aligned and return_aligned and sharpe_aligned and drawdown_aligned

        return PaperTradingComparison(
            backtest_win_rate_pct=backtest_result.win_rate_pct,
            paper_win_rate_pct=paper_result.win_rate_pct,
            backtest_annualized_return_pct=backtest_result.annualized_return_pct,
            paper_annualized_return_pct=paper_result.annualized_return_pct,
            backtest_sharpe_ratio=backtest_result.sharpe_ratio,
            paper_sharpe_ratio=paper_result.sharpe_ratio,
            backtest_max_drawdown_pct=backtest_result.max_drawdown_pct,
            paper_max_drawdown_pct=paper_result.max_drawdown_pct,
            backtest_profit_factor=backtest_result.profit_factor,
            paper_profit_factor=paper_result.profit_factor,
            win_rate_aligned=win_rate_aligned,
            return_aligned=return_aligned,
            sharpe_aligned=sharpe_aligned,
            drawdown_aligned=drawdown_aligned,
            overall_aligned=overall,
        )

    def analyze_divergence(
        self,
        session: PaperTradingSession,
        backtest_record: BacktestRecord,
    ) -> str:
        """LLM-driven analysis of why paper trading diverges from backtest results."""
        strategy = session.strategy
        bt = backtest_record.result
        pt = session.result

        if pt is None:
            return "No paper trading results available for analysis."

        bt_trades_sample = backtest_record.trades[:10]
        pt_trades_sample = session.trades[:20]

        prompt = _DIVERGENCE_PROMPT.format(
            asset_class=strategy.asset_class,
            hypothesis=strategy.hypothesis,
            signal_definition=strategy.signal_definition,
            entry_rules="; ".join(strategy.entry_rules),
            exit_rules="; ".join(strategy.exit_rules),
            bt_start=backtest_record.config.start_date,
            bt_end=backtest_record.config.end_date,
            bt_annual_return=bt.annualized_return_pct,
            bt_win_rate=bt.win_rate_pct,
            bt_sharpe=bt.sharpe_ratio,
            bt_max_dd=bt.max_drawdown_pct,
            bt_profit_factor=bt.profit_factor,
            bt_trade_count=len(backtest_record.trades),
            pt_start=session.data_period_start,
            pt_end=session.data_period_end,
            pt_annual_return=pt.annualized_return_pct,
            pt_win_rate=pt.win_rate_pct,
            pt_sharpe=pt.sharpe_ratio,
            pt_max_dd=pt.max_drawdown_pct,
            pt_profit_factor=pt.profit_factor,
            pt_trade_count=len(session.trades),
            bt_trades_text=_format_trades_table(bt_trades_sample),
            pt_trades_text=_format_trades_table(pt_trades_sample),
        )

        result = self._divergence_agent(prompt)
        raw = str(result).strip()
        data = json.loads(raw)
        return str(data.get("analysis", "Divergence analysis not available."))
